# amber/protocols/base.py
"""The base class of every protocol."""

from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .. import backscatter


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
    """What a base station sends and how a Tag answers.

    A subclass lists its parameters with their defaults in PARAMS, returns the base station's
    schedule or policy from reader_args(), and overrides the rest only where it differs. The
    scenario builds every Tag as TAG_CLASS, then calls setup_tag() on it, and every base station
    as a BSBehavior, then calls setup_reader() on it.

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
    #: The class every Tag is built as.
    TAG_CLASS = backscatter.BackscatterModule

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

    def check(self) -> None:
        """Check the parameters beyond their types; raise ValueError naming the one that is wrong."""

    def reader_args(self) -> Dict[str, Any]:
        """The BSBehavior arguments that make this protocol: schedule and loop, or policy."""
        raise NotImplementedError

    def setup_reader(self, bs) -> None:
        """Prepare a newly built BSBehavior, before the simulation starts."""

    def setup_tag(self, bsm) -> None:
        """Prepare a newly built Tag (a TAG_CLASS), before the simulation starts."""

    def outputs(self, bss) -> Dict[str, Tuple[Sequence[str], List[Sequence[Any]]]]:
        """Extra CSV files a run writes from its base stations, as {file name: (columns, rows)}."""
        return {}
