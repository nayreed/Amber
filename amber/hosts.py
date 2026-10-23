# amber/hosts.py
"""The Tag host: each Tag owns a SimPy environment for its energy (capacitor, controller and
sensing) and advances it one millisecond at a time. amber.iq drives it, one block of IQ samples
per millisecond, as amber-tag does in its OAI container (through amber.session) and as
python -m amber.run does on one machine. The Reader needs no SimPy: its protocol, in the A-IoT MAC
library (amber.mac), chooses 38.391 messages that amber.iq runs on the waveforms.

Per millisecond n, a Tag:
  1. TagHost.deliver(n)    advances its environment to n;
  2. amber.iq              decodes the Reader's R2D from the carrier while the Tag is on, runs the
                           38.391 device MAC and starts its D2R reflection when the MAC says so;
  3. TagHost.step(n)       processes its events at n (the Controller draws current, senses).

The host runs on a PhasedEnvironment, which processes the events of one millisecond in a fixed
order: those of the objects the Tags share, then each node's in NODE_IDS order. Plain SimPy orders
events of the same time by when they were scheduled; a run could then differ between the
containers and one machine.

A scenario is a TOML file read by amber.scenario, or a Python module defining:
  phy                            the LinkConfig of the A-IoT link (amber.link)
  protocol                       the Reader's protocol (amber.protocols)
  SIM_TIME_MS                    simulated duration in ms
  NODE_IDS                       node ids, in the order the Tags are built
  BS_IDS                         base station ids (one)
  build_shared(env)              objects all Tags share (energy source, coverage map); returns them
  build_tag(env, shared, id)     one node's Capacitor, AiotTag and Controller; returns the AiotTag
  iq_links()                     {node id: (sector, subcarrier_shift, power)} as Scenario.iq_links
  reader_noise_figure_db         the Reader's receiver noise figure
  enable_sic                     whether the Reader cancels decoded D2Rs and looks again
and optionally:
  SEED                           seed for Python's global random generator, set before building.

Give every node its own random generator (the rng argument of Controller). A node drawing from
the global generator gets numbers that depend on what every other node on the same environment
drew, so a Tag in its own container could not match a run on one machine.
"""

import contextlib
import importlib.util
import random
import weakref
from heapq import heappush
from typing import Any, Iterator, List, Optional, Sequence

import simpy
from simpy.events import NORMAL, Initialize, Interruption

from .scenario import load as load_toml_scenario

_SCENARIO_ATTRS = ("SIM_TIME_MS", "NODE_IDS", "BS_IDS", "build_shared", "build_tag", "phy", "protocol",
                   "iq_links", "reader_noise_figure_db", "enable_sic")


def load_scenario(path: str) -> Any:
    """Read a scenario: a TOML file (see amber.scenario), or a Python module, which is imported
    and checked to define the expected names."""
    if path.endswith(".toml"):
        return load_toml_scenario(path)
    spec = importlib.util.spec_from_file_location("amber_scenario", path)
    if spec is None:
        raise ValueError(f"cannot load scenario {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    missing = [a for a in _SCENARIO_ATTRS if not hasattr(module, a)]
    if missing:
        raise ValueError(f"scenario {path} does not define {', '.join(missing)}")
    return module


def _seed(scenario: Any):
    if hasattr(scenario, "SEED"):
        random.seed(scenario.SEED)


READER_PHASE = 0
SHARED_PHASE = 1
_RUN_PHASE = -1  # the stop event of env.run(until=...), which plain SimPy also processes first


def node_phase(scenario: Any, node_id: int) -> int:
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


def _advance_to(env: simpy.Environment, ms: int):
    """Process every event before ms and set the clock to ms."""
    if env.now < ms:
        env.run(until=ms)


def _run_current_ms(env: simpy.Environment):
    """Process every event at the current time, including those scheduled while doing so."""
    now = env.now
    while env.peek() == now:
        env.step()


class TagHost:
    """Runs one node of the scenario."""

    def __init__(self, scenario: Any, node_id: int):
        if node_id not in scenario.NODE_IDS:
            raise ValueError(f"node {node_id} is not in the scenario's NODE_IDS {scenario.NODE_IDS}")
        self.scenario = scenario
        self.node_id = node_id
        _seed(scenario)
        self.env = PhasedEnvironment()
        with self.env.in_phase(SHARED_PHASE):
            shared = scenario.build_shared(self.env)
        with self.env.in_phase(node_phase(scenario, node_id)):
            self.bsm = scenario.build_tag(self.env, shared, node_id)

    def deliver(self, ms: int, _unused=()):
        """Advance to ms, before this node's events at ms."""
        _advance_to(self.env, ms)
        assert self.env.now == ms, f"deliver({ms}) called at {self.env.now} ms"

    def step(self, ms: int):
        """Process this node's events at ms."""
        assert self.env.now == ms, f"step({ms}) called at {self.env.now} ms"
        _run_current_ms(self.env)
