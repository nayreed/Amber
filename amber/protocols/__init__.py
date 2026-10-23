# amber/protocols/__init__.py
"""Protocols: what a Reader sends, chosen by name in a scenario file.

Everything on the air is 3GPP Rel-19 Ambient IoT. The Tag side is fixed by TS 38.391 (the A-IoT
MAC, amber.mac) and is the same for every protocol. A protocol is only a Reader policy:
it decides what 38.391 leaves to the Reader (whom to page, with how many access occasions, when to
send an Access Trigger, which devices to answer in a Random ID Response, when to ask a known AS ID
for data, when to NACK) and what goes in the Data SDU. It cannot add messages or change how Tags
behave.

A protocol is a subclass of Protocol, exported as PROTOCOL by its module. A scenario names one of
Amber's own by its module name (``broadcast``, ``unicast``, ``registration``,
``adaptive_aloha``, ``gen2_q``) or any other by its full module path, such as
``my_package.my_protocol``.

Its ``policy(ctx)`` is a generator. It yields one 38.391 R2D message at a time, built with
``ctx.mac`` (a ReaderMAC) and the Scheduling Info helpers of ``ctx.cfg`` (``cbra``, ``ridr``,
``cfa``, ``unicast``), and receives a ``link.Heard`` after each: the D2R answers decoded and
the number of collisions. The Reader sends the next message as soon as every answer the last one
can cause has arrived. ``Protocol.access`` is a helper for ``yield from``: a paging or Access
Trigger, then a Random ID Response to the Msg1s it brought.

amber/protocols/README.md is the guide: what each protocol here does, its parameters, how to
write a new one, and the pitfalls. tests/test_protocols.py and tests/test_protocol_conformance.py
run the protocols here.
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
