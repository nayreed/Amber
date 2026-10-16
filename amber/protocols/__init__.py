# amber/protocols/__init__.py
"""Protocols: what a base station sends and how a Tag answers, chosen by name in a scenario file.

A protocol is a subclass of Protocol, exported as PROTOCOL by its module. A scenario names one of
Amber's own by its module name (``broadcast``, ``unicast``, ``registration``,
``adaptive_aloha``, ``gen2_q``) or any other by its full module path, such as
``my_package.my_protocol``.

A base station runs either a static schedule or a policy: a generator that yields one frame at a
time, each a list of (mode, duration_ms, slot_id[, payload]) entries. The policy resumes once the
whole frame has played out, and can then read on the BSBehavior:

- ``decoded_this_frame``: the node ids decoded in this frame;
- ``slot_results``: one SlotResult per listening slot of this frame, empty slots included, with the
  node ids it decoded, how many packets collided, and ``empty``, ``success`` and ``collision``
  (a slot can hold a decoded and a collided packet at once, which ``collision`` counts);
- ``total_collided``: collided packets since the start of the run.

A protocol has two shapes. One command and many slots per frame (``adaptive_aloha``) decides once
per frame. One command and one slot per frame (``gen2_q``) decides after every slot, at the cost
of a command before every slot, as Gen2's QueryRep is.

A new protocol goes in this package, and must run the same split across processes (the Reader
and each Tag in its own, as OAI's amber-reader and amber-tag run it) as in one process:

- a Tag draws its random numbers from ``self.rng``, its own generator, never from the random
  module, whose numbers depend on what else ran in the same process;
- command payloads and what a Tag sends are JSON values (numbers, strings, lists, dicts), and
  every duration is a whole number of milliseconds.

tests/split/test_protocol_conformance.py finds every module here and checks both, by running it
with its default parameters in one process and split, and by reading it for the random module.
"""

import importlib
from typing import Any, Mapping, Sequence

from .base import Protocol

__all__ = ["Protocol", "load"]


def load(name: str, params: Mapping[str, Any], node_ids: Sequence[int]) -> Protocol:
    """Import the protocol `name` and make it with the scenario's parameters and Tags.

    Parameters
    ----------
    name : str
        A module of amber.protocols, or the full path of a module that defines PROTOCOL.
    params : Mapping[str, Any]
        The protocol's parameters from the scenario; any it does not take is an error.
    node_ids : Sequence[int]
        The scenario's Tags, in order.

    Returns
    -------
    Protocol
        The protocol, ready to build its base stations and Tags.
    """
    module_name = name if "." in name else f"{__name__}.{name}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        # Only a missing protocol module is the scenario's mistake; a missing import inside it is not.
        if e.name is None or not (module_name == e.name or module_name.startswith(e.name + ".")):
            raise
        raise ValueError(f"unknown protocol {name!r}: no module {module_name}") from None
    cls = getattr(module, "PROTOCOL", None)
    if not (isinstance(cls, type) and issubclass(cls, Protocol)):
        raise ValueError(f"protocol module {module_name} does not define PROTOCOL as a Protocol subclass")
    return cls(params, node_ids)
