# amber/split/hosts.py
"""Reader host and Tag host: each owns a SimPy environment and advances it one millisecond at a
time, exchanging Deliveries and Uplinks with the other side in between.

Per millisecond n, the exchange is:
  1. ReaderHost.run_ms(n)        Reader processes its events at n; returns the commands it sent.
  2. TagHost.deliver(n, cmds)    each Tag receives its commands at n, before its own events at n.
  3. TagHost.step(n)             each Tag processes its events at n; returns its transmissions.
  4. ReaderHost.apply_uplinks()  the Reader receives them at n through Amber's own interrupt path.
This is the order one-process Amber uses when the Reader's event at n was scheduled before the
Tags' (the usual case); test_split_equivalence checks it against one-process runs.

Amber times must be whole milliseconds: a command or transmission at any other time is refused
rather than silently moved to the next millisecond.

A scenario is a Python module defining:
  SIM_TIME_MS                    simulated duration, as passed to env.run(until=...)
  NODE_IDS                       node ids, in the order one-process Amber builds them
  BS_IDS                         base station ids
  build_shared(env)              objects all Tags share (energy source, coverage map); returns them
  build_tag(env, shared, id)     one node's Capacitor, BackscatterModule and Controller; returns
                                 the BackscatterModule (its bs_processes are set by the host)
  build_reader(env, tags)        the BSBehavior list, given the Tags as backscatter_modules
and optionally:
  SEED                           seed for Python's random generator, set before building. Amber
                                 draws from that one generator for all nodes, so a split run
                                 matches a one-process run only when each process has one node.
"""

import importlib.util
import random
from types import ModuleType
from typing import Any, List, Sequence, Tuple

import simpy

from .proxies import BSProxy, Delivery, TagProxy, Uplink

_SCENARIO_ATTRS = ("SIM_TIME_MS", "NODE_IDS", "BS_IDS", "build_shared", "build_tag", "build_reader")


def load_scenario(path: str) -> ModuleType:
    """Import a scenario module from a file path and check it defines the expected names."""
    spec = importlib.util.spec_from_file_location("amber_scenario", path)
    if spec is None:
        raise ValueError(f"cannot load scenario {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    missing = [a for a in _SCENARIO_ATTRS if not hasattr(module, a)]
    if missing:
        raise ValueError(f"scenario {path} does not define {', '.join(missing)}")
    return module


def _seed(scenario: ModuleType):
    if hasattr(scenario, "SEED"):
        random.seed(scenario.SEED)


def build_one_process(scenario: ModuleType, env: simpy.Environment) -> Tuple[List[Any], List[Any]]:
    """Build the whole scenario in one environment, in the same order as Amber's demos (shared
    objects, then each node, then the base stations). Returns (bs_behaviors, backscatter_modules)."""
    _seed(scenario)
    shared = scenario.build_shared(env)
    bsms = [scenario.build_tag(env, shared, node_id) for node_id in scenario.NODE_IDS]
    bss = scenario.build_reader(env, bsms)
    for bsm in bsms:
        bsm.bs_processes = list(bss)
    return bss, bsms


def _advance_to(env: simpy.Environment, ms: int):
    """Process every event before ms and set the clock to ms."""
    if env.now < ms:
        env.run(until=ms)


def _run_current_ms(env: simpy.Environment):
    """Process every event at the current time, including those scheduled while doing so."""
    now = env.now
    while env.peek() == now:
        env.step()


class ReaderHost:
    """Runs the scenario's base stations, with TagProxy objects in place of the Tags."""

    def __init__(self, scenario: ModuleType):
        self.scenario = scenario
        _seed(scenario)
        self.env = simpy.Environment()
        self._deliveries: List[Delivery] = []
        self.tags = [TagProxy(node_id, self._deliveries) for node_id in scenario.NODE_IDS]
        self.bss = scenario.build_reader(self.env, self.tags)
        self._bs_by_id = {bs.id: bs for bs in self.bss}
        if sorted(self._bs_by_id) != sorted(scenario.BS_IDS):
            raise ValueError(f"build_reader returned base stations {sorted(self._bs_by_id)}, BS_IDS is {scenario.BS_IDS}")

    def run_ms(self, ms: int) -> List[Delivery]:
        """Process the Reader's events at ms; return the commands sent to Tags at ms."""
        _advance_to(self.env, ms)
        assert self.env.now == ms, f"run_ms({ms}) called at {self.env.now} ms"
        if self._deliveries:
            raise ValueError(f"a command was sent between {ms - 1} and {ms} ms; Amber times must be whole milliseconds")
        _run_current_ms(self.env)
        out = list(self._deliveries)
        self._deliveries.clear()
        return out

    def apply_uplinks(self, ms: int, uplinks: Sequence[Uplink]):
        """Hand the Tags' transmissions at ms to the base stations, exactly as
        BackscatterModule.do_transmit() does in one-process Amber."""
        assert self.env.now == ms, f"apply_uplinks({ms}) called at {self.env.now} ms"
        for up in uplinks:
            bs = self._bs_by_id.get(up.bs_id)
            if bs is None:
                raise ValueError(f"node {up.node_id} transmitted to unknown base station {up.bs_id}")
            bs.received_tag_id = up.node_id
            bs.data_from_tag = up.payload
            bs.rssi_dbm = up.rssi_dbm
            bs.best_sector_idx = up.sector_idx
            bs.subcarrier_shift = up.subcarrier_shift
            bs.action.interrupt("receive_data")
            _run_current_ms(self.env)
        if self._deliveries:
            raise ValueError(f"a base station sent a command while receiving at {ms} ms")


class TagHost:
    """Runs one node of the scenario, with BSProxy objects in place of the base stations."""

    def __init__(self, scenario: ModuleType, node_id: int):
        if node_id not in scenario.NODE_IDS:
            raise ValueError(f"node {node_id} is not in the scenario's NODE_IDS {scenario.NODE_IDS}")
        self.scenario = scenario
        self.node_id = node_id
        _seed(scenario)
        self.env = simpy.Environment()
        self._uplinks: List[Uplink] = []
        self.bss = [BSProxy(bs_id, self._uplinks) for bs_id in scenario.BS_IDS]
        shared = scenario.build_shared(self.env)
        self.bsm = scenario.build_tag(self.env, shared, node_id)
        self.bsm.bs_processes = list(self.bss)

    def deliver(self, ms: int, deliveries: Sequence[Delivery]):
        """Advance to ms and hand this node the commands the Reader sent at ms."""
        _advance_to(self.env, ms)
        assert self.env.now == ms, f"deliver({ms}) called at {self.env.now} ms"
        if self._uplinks:
            raise ValueError(f"node {self.node_id} transmitted between {ms - 1} and {ms} ms; Amber times must be whole milliseconds")
        for d in deliveries:
            assert d.node_id == self.node_id, f"node {self.node_id} got a command for node {d.node_id}"
            self.bsm.receive_command(d.cmd, d.bs_id, d.data)

    def step(self, ms: int) -> List[Uplink]:
        """Process this node's events at ms; return what it transmitted at ms."""
        assert self.env.now == ms, f"step({ms}) called at {self.env.now} ms"
        _run_current_ms(self.env)
        out = list(self._uplinks)
        self._uplinks.clear()
        return out
