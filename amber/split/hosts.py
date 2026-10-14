# amber/split/hosts.py
"""Reader host and Tag host: each owns a SimPy environment and advances it one millisecond at a
time, exchanging Deliveries and Uplinks with the other side in between.

Per millisecond n, the exchange is:
  1. ReaderHost.run_ms(n)        Reader processes its events at n; returns the commands it sent.
  2. TagHost.deliver(n, cmds)    each Tag receives its commands at n, before its own events at n.
  3. TagHost.step(n)             each Tag processes its events at n; returns its transmissions.
  4. ReaderHost.apply_uplinks()  the Reader receives them at n through Amber's own interrupt path.

Both hosts run on a PhasedEnvironment, which processes the events of one millisecond in a fixed
order: the Reader's, then those of the objects the Tags share, then each node's in NODE_IDS order.
build_one_process() runs the whole scenario on the same kind of environment; it is the reference
a split run equals, which test_split_equivalence checks. It differs from steps 1 to 4 only in when
the Reader handles a transmission: right after the node made it, before the next node's events.
That cannot change what the next node does, since the Reader may not send a command while
receiving (apply_uplinks refuses it).

Plain SimPy, which Amber's demos use, orders events of the same time by when they were
scheduled, mixing the Reader's and the Tags'. A one-process run on it can differ from a split run
when a Reader event and a Tag event fall in the same millisecond, for example a transmission
exactly at the end of a Reader listening window.

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
  SEED                           seed for Python's global random generator, set before building.

Give every node its own random generator (the rng argument of BackscatterModule and Controller).
A node drawing from the global generator gets numbers that depend on what every other node in
the same process drew, so a run split across processes cannot match a one-process run.
"""

import contextlib
import importlib.util
import random
import weakref
from heapq import heappush
from types import ModuleType
from typing import Any, Iterator, List, Optional, Sequence, Tuple

import simpy
from simpy.events import NORMAL, Initialize, Interruption

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


READER_PHASE = 0
SHARED_PHASE = 1
_RUN_PHASE = -1  # the stop event of env.run(until=...), which plain SimPy also processes first


def node_phase(scenario: ModuleType, node_id: int) -> int:
    """The phase of a node's events: nodes come after the Reader and the shared objects, in NODE_IDS order."""
    return 2 + list(scenario.NODE_IDS).index(node_id)


class PhasedEnvironment(simpy.Environment):
    """A SimPy environment that orders the events of the same time by phase, then as SimPy does
    (priority, then the order they were scheduled in).

    The phase of an event is the phase of the process it interrupts, for an interrupt; otherwise
    the phase of the running process, or when no process runs, of the event being processed, or
    of the enclosing in_phase() block. A new process takes the phase it is created in.

    This relies on the layout of SimPy 4's event queue: (time, priority, id, event) tuples, with
    the priority here replaced by (phase, priority).
    """

    def __init__(self):
        super().__init__()
        self._process_phase: "weakref.WeakKeyDictionary[simpy.Process, int]" = weakref.WeakKeyDictionary()
        self._block_phase: Optional[int] = None
        self._step_phase: Optional[int] = None

    @contextlib.contextmanager
    def in_phase(self, phase: int) -> Iterator[None]:
        """Give the processes and events created in this block, outside any process, this phase."""
        outer, self._block_phase = self._block_phase, phase
        try:
            yield
        finally:
            self._block_phase = outer

    def _current_phase(self) -> int:
        if self.active_process is not None:
            return self._process_phase[self.active_process]
        if self._step_phase is not None:
            return self._step_phase
        if self._block_phase is not None:
            return self._block_phase
        raise RuntimeError("an event was scheduled outside any process and any in_phase() block, so its phase is unknown")

    def schedule(self, event: simpy.Event, priority=NORMAL, delay=0) -> None:
        if isinstance(event, Interruption):
            phase = self._process_phase[event.process]
        else:
            phase = self._current_phase()
            if isinstance(event, Initialize):
                # The first event of a new process, which resumes it.
                self._process_phase[event.callbacks[0].__self__] = phase
        heappush(self._queue, (self._now + delay, (phase, priority), next(self._eid), event))

    def step(self) -> None:
        self._step_phase = self._queue[0][1][0] if self._queue else None
        try:
            super().step()
        finally:
            self._step_phase = None

    def run(self, until=None):
        with self.in_phase(_RUN_PHASE):
            return super().run(until)


def build_one_process(scenario: ModuleType) -> Tuple[PhasedEnvironment, List[Any], List[Any]]:
    """Build the whole scenario in one environment, in the same order as Amber's demos (shared
    objects, then each node, then the base stations). Returns (env, bs_behaviors,
    backscatter_modules); run it with env.run(until=scenario.SIM_TIME_MS)."""
    _seed(scenario)
    env = PhasedEnvironment()
    with env.in_phase(SHARED_PHASE):
        shared = scenario.build_shared(env)
    bsms = []
    for node_id in scenario.NODE_IDS:
        with env.in_phase(node_phase(scenario, node_id)):
            bsms.append(scenario.build_tag(env, shared, node_id))
    with env.in_phase(READER_PHASE):
        bss = scenario.build_reader(env, bsms)
    for bsm in bsms:
        bsm.bs_processes = list(bss)
    return env, bss, bsms


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
        self.env = PhasedEnvironment()
        self._deliveries: List[Delivery] = []
        self.tags = [TagProxy(node_id, self._deliveries) for node_id in scenario.NODE_IDS]
        with self.env.in_phase(READER_PHASE):
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
        self.env = PhasedEnvironment()
        self._uplinks: List[Uplink] = []
        self.bss = [BSProxy(bs_id, self._uplinks) for bs_id in scenario.BS_IDS]
        with self.env.in_phase(SHARED_PHASE):
            shared = scenario.build_shared(self.env)
        with self.env.in_phase(node_phase(scenario, node_id)):
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
