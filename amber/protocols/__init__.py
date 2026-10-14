# amber/protocols/__init__.py
"""Protocols: what a base station sends and how a Tag answers, chosen by name in a scenario file.

A protocol is a subclass of Protocol, exported as PROTOCOL by its module. A scenario names one of
Amber's own by its module name (``broadcast``, ``unicast``, ``registration``,
``adaptive_aloha``) or any other by its full module path, such as ``my_package.my_protocol``.
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
