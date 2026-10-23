# amber/protocols/base.py
"""The base class of every protocol: a Reader policy over 38.391 messages."""

from typing import Any, Dict, Generator, List, Mapping, Sequence, Tuple

from ..mac import pdu as mp
from ..mac import Msg1Heard


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "an integer"
    if isinstance(value, float):
        return "a number"
    return f"a {type(value).__name__}"


def _same_kind(default: Any, value: Any) -> bool:
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float))
    return isinstance(value, type(default))


class Protocol:
    """What the Reader sends, as a policy over 38.391 (Rel-19 A-IoT MAC) messages.

    The Tag side is fixed by 38.391 and is the same for every protocol (amber.mac). A
    protocol decides only what the spec leaves to the Reader: whom to page and how (CBRA with how
    many access occasions, or CFA to one Paging ID), when to send an Access Trigger, which devices
    to answer in a Random ID Response, when to ask a known AS ID for data, when to NACK.

    policy(ctx) is a generator. It yields one R2D message at a time (build them with ctx.mac and
    the Scheduling Info helpers of ctx.cfg) and receives what that message brought back, a
    link.Heard: the D2R answers decoded (Msg1s, or Tag data) and the collisions. The Reader
    sends the next message as soon as every answer the last one can cause has arrived.

    Parameters
    ----------
    params : Mapping[str, Any]
        Values for some of PARAMS; the others keep their defaults.
    node_ids : Sequence[int]
        The scenario's Tags, in order.
    """

    #: Parameter names and their defaults. A value must be of its default's type (an integer
    #: where the default is an integer, any number where it is a float).
    PARAMS: Dict[str, Any] = {}

    def __init__(self, params: Mapping[str, Any], node_ids: Sequence[int]):
        unknown = sorted(set(params) - set(self.PARAMS))
        if unknown:
            takes = ", ".join(self.PARAMS) or "none"
            raise ValueError(f"protocol {self.name} has no parameter {', '.join(unknown)} (it takes {takes})")
        for key, value in params.items():
            if not _same_kind(self.PARAMS[key], value):
                raise ValueError(f"protocol {self.name}: {key} must be {_kind(self.PARAMS[key])}, got {value!r}")
        self.params: Dict[str, Any] = {**self.PARAMS, **params}
        self.node_ids: List[int] = list(node_ids)
        self.check()

    @property
    def name(self) -> str:
        """The protocol's name, as a scenario gives it."""
        module = type(self).__module__
        return module[len(__package__) + 1:] if module.startswith(__package__ + ".") else module

    def positive(self, *keys: str) -> None:
        """Raise ValueError unless every parameter in keys is at least 1."""
        for key in keys:
            if self.params[key] < 1:
                raise ValueError(f"protocol {self.name}: {key} must be at least 1, got {self.params[key]}")

    def within(self, key: str, lo, hi) -> None:
        if not lo <= self.params[key] <= hi:
            raise ValueError(f"protocol {self.name}: {key} must be within [{lo}, {hi}], got {self.params[key]}")

    def check(self) -> None:
        """Check the parameters beyond their types; raise ValueError naming the one that is wrong."""

    def policy(self, ctx) -> Generator:
        """The Reader's policy (see the class docstring)."""
        raise NotImplementedError

    def outputs(self) -> Dict[str, Tuple[Sequence[str], List[Sequence[Any]]]]:
        """Extra CSV files a run writes, as {file name: (columns, rows)}."""
        return {}

    # -- building blocks for policies ----------------------------------------------------------
    @staticmethod
    def access(ctx, msg):
        """Send a CBRA paging or Access Trigger, then answer the Msg1s it brought with one Random
        ID Response, which brings each answered Tag's data. Use as `h1, h2 = yield from
        self.access(ctx, msg)`; h2 is None when no Msg1 decoded."""
        h1 = yield msg
        heard = [Msg1Heard(a.message.random_id, a.r_sfs, a.slot) for a in h1.answers if a.kind == "msg1"]
        if not heard:
            return h1, None
        heard = heard[:ctx.cfg.max_entries]
        h2 = yield ctx.mac.random_id_response(heard, ctx.cfg.ridr(len(heard)))
        return h1, h2

    @staticmethod
    def occasions_code(n: int) -> int:
        """The Number of Access Occasions codepoint v (n = 2^v) for at least n occasions."""
        v = max(0, (int(n) - 1).bit_length())
        if v > 15:
            raise ValueError(f"{n} access occasions is more than the 2^15 a paging can offer")
        return v

    @staticmethod
    def r2d_per_paging(ctx, v: int, sched: mp.SchedulingInfo) -> int:
        """R2D messages (the paging and its Access Triggers) to play out 2^v occasions."""
        m = sched.x * len(sched.r_sfs)
        return -(-(1 << v) // m)
