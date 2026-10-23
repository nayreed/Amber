# amber/protocols/__init__.py
"""Protocols: what a Reader sends, chosen by name in a scenario file.

Everything on the air is 3GPP Rel-19 Ambient IoT. The Tag side is fixed by TS 38.391 and is the
same for every protocol. A protocol is only a Reader policy: it decides what 38.391 leaves to the
Reader (whom to page, with how many access occasions, when to send an Access Trigger, which devices
to answer in a Random ID Response, when to ask a known AS ID for data, when to NACK).

The protocols are part of the A-IoT MAC library (OAI's openair2/LAYER2/AIOT_MAC, in
libaiot_phy.so), the same code a gNB or a UE runs as the Reader: ``registration``, ``broadcast``,
``unicast``, ``gen2_q`` and ``adaptive_aloha``. A new protocol is written there, in C
(aiot_protocols.c); amber/protocols/README.md says what each one does and how to add one.
"""

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import mac

__all__ = ["Protocol", "load", "names"]


# sends only what a caller pushes (amber.mac.Reader.push): nothing a scenario could run
CALLER_DRIVEN = {"manual"}


def names() -> List[str]:
    """The protocols a scenario can name."""
    return [n for n in mac.protocol_names() if n not in CALLER_DRIVEN]


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "an integer"
    if isinstance(value, float):
        return "a number"
    return f"a {type(value).__name__}"


class Protocol:
    """A protocol of the MAC library, as a scenario names it, with its parameters and Tags.

    reader(cfg) makes the Reader that runs it on a link; outputs() gives the files its table
    adds to a run (q.csv for gen2_q, adaptation.csv for adaptive_aloha)."""

    def __init__(self, name: str, params: Mapping[str, Any], node_ids: Sequence[int]):
        if name not in names():
            raise ValueError(f"unknown protocol {name!r}: the A-IoT MAC has {', '.join(sorted(names()))}")
        spec = mac.protocol_params(name)
        self.name = name
        unknown = sorted(set(params) - set(spec))
        if unknown:
            takes = ", ".join(spec) or "none"
            raise ValueError(f"protocol {name} has no parameter {', '.join(unknown)} (it takes {takes})")
        for key, value in params.items():
            whole = spec[key][1]
            ok = not isinstance(value, bool) and (isinstance(value, int) if whole else isinstance(value, (int, float)))
            if not ok:
                raise ValueError(f"protocol {name}: {key} must be {'an integer' if whole else 'a number'}, "
                                 f"got {value!r}")
        self.params: Dict[str, Any] = {k: (int(d) if whole else d) for k, (d, whole) in spec.items()}
        self.params.update(params)
        self.node_ids: List[int] = list(node_ids)
        self._reader: Optional[mac.Reader] = None

    def check(self, cfg) -> None:
        """Raise ValueError if the protocol cannot run on the link cfg with these parameters."""
        mac.Reader(cfg, self.name, self.params, self.node_ids)

    def reader(self, cfg) -> "mac.Reader":
        """The Reader that runs this protocol on the link cfg; outputs() reads its table."""
        self._reader = mac.Reader(cfg, self.name, self.params, self.node_ids)
        return self._reader

    def outputs(self) -> Dict[str, Tuple[Sequence[str], List[Sequence[Any]]]]:
        """Extra CSV files a run writes, as {file name: (columns, rows)}."""
        t = self._reader.table() if self._reader is not None else None
        return {t[0]: (tuple(t[1]), t[2])} if t else {}


def load(name: str, params: Mapping[str, Any], node_ids: Sequence[int], cfg=None) -> Protocol:
    """The protocol `name` with the scenario's parameters and Tags, checked by the MAC library on
    the link settings cfg (amber.link.DEFAULT_LINK when None)."""
    p = Protocol(name, params, node_ids)
    if cfg is None:
        from ..link import DEFAULT_LINK
        cfg = DEFAULT_LINK
    p.check(cfg)
    return p
