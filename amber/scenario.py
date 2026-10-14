# amber/scenario.py
"""Scenarios as TOML files: the values of a simulation, kept apart from the protocol it runs.

A scenario file says what is simulated (base stations, Tags, radio, energy, duration, seed) and
names the protocol that drives it (see amber.protocols). load() reads one into a Scenario, which
amber.split runs in one process (amber.split.reference) or split into a Reader and one process
per Tag (amber-reader and amber-tag in OAI).

Every key is named as the field of the Amber class it sets, and every value that a file leaves
out keeps that class's default. demo_experiments/<demo>/<demo>.toml are complete examples. The
tables:

```text
[run]
    sim_time_ms     simulated duration in ms (required)
    seed            integer from which every random draw comes, each Tag's from its own
                    generator; without one, load() draws a new seed, so every run is a new sample
[reader]
    protocol        the protocol (required): a module of amber.protocols, or a full module path
    params          table of the protocol's parameters
    enable_sic, required_sinr_db, cancellation_factor, noise_figure_db, bandwidth_hz
                    BSBehavior's options
[[base_station]]    one or more: id, x, y (required) and BaseStation's other fields, and sectors,
                    a list of tables of Sector's fields (azimuth_deg and beamwidth_deg required)
[radio]             CoverageMap's options: freq_hz, pathloss_model, los, extra_np_per_m,
                    bandwidth_hz, noise_figure_db, combine_mode
[energy]            the energy trace (EnvEnergySource), needed by "external" and "hybrid" Tags:
                    trace (path relative to the scenario file, required), column, resistance
[tag_defaults]      what every Tag group starts from: any key of a group but its placement
[[tag_group]]       one or more groups of Tags that share their settings:
    energy_mode     "wpt", "external" or "hybrid" (required)
    Node's fields   height, sensitivity_dbm, efficiency, antenna_type, antenna_gain_dbi, ...
    capacitor       table: dt, R_series, R_leakage, C (CapacitorParams), initial_voltage, voltage_max
    controller      table: currents and durations_ms, tables of listening, sensing, processing and
                    transmitting; thresholds_v, a table of high and low; max_startup_time_ms
    positions       list of [x, y], one per Tag; or else
    count, layout   that many Tags placed by layout = {kind = "ring", r_min, r_max, seed, center}:
                    each at a distance drawn uniformly from [r_min, r_max], then an angle drawn
                    uniformly from [0, 2 pi), around center ([0, 0] if not given), with
                    random.Random(seed); without a seed, one derived from the run's
    ids             the Tags' ids; by default the integers after the highest id so far, from 0
```

A group's capacitor and controller tables are merged key by key over those of [tag_defaults].
The Tags' order, which is the order of a run's events within a millisecond, is the groups' order,
then the order within each group.

load() also takes overrides, {dotted key: value}, applied to the file before it is read, such as
{"run.seed": 7, "reader.params.slots": 4, "tag_group.0.count": 20}.
"""

import dataclasses
import math
import os
import random
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

from . import bsengine, capacitor, controller, energy, propagation, protocols, radiodevices

_ABSENT = object()

# BSBehavior options a scenario may set; the protocol sets schedule, loop and policy.
_READER_OPTIONS = {"enable_sic": "bool", "required_sinr_db": "number", "cancellation_factor": "number",
                   "noise_figure_db": "number", "bandwidth_hz": "number"}
_ENERGY_MODES = ("wpt", "external", "hybrid")
_TOP_TABLES = ("run", "reader", "base_station", "radio", "energy", "tag_defaults", "tag_group")
_PLACEMENT = ("positions", "count", "layout", "ids")


def _kind_of(annotation: str) -> str:
    """The value kind of a dataclass field, from its annotation (these modules use string annotations)."""
    a = str(annotation)
    if a.startswith("Optional[") and a.endswith("]"):
        a = a[len("Optional["):-1]
    if a == "bool":
        return "bool"
    if a == "int":
        return "int"
    if a == "float":
        return "number"
    if a == "str" or a.endswith("| str"):
        return "str"
    raise TypeError(f"no scenario kind for annotation {annotation!r}")


def _fields(cls, exclude: Sequence[str] = ()) -> Dict[str, str]:
    """{field name: kind} of a dataclass's constructor arguments, but those in exclude."""
    return {f.name: _kind_of(f.type) for f in dataclasses.fields(cls) if f.init and f.name not in exclude}


_NODE_FIELDS = _fields(radiodevices.Node, exclude=("id", "x", "y"))
_SECTOR_FIELDS = _fields(radiodevices.Sector)
_BS_FIELDS = _fields(radiodevices.BaseStation, exclude=("sectors",))
_RADIO_FIELDS = _fields(propagation.CoverageMap,
                        exclude=("base_stations", "nodes", "node_energy_mode", "node_ext_power_fn"))
_CAP_PARAMS_FIELDS = _fields(capacitor.CapacitorParams)
_CAP_FIELDS = _fields(capacitor.Capacitor, exclude=("env", "id", "params", "keep_logs"))
_CONTROLLER_TABLES = {"currents": controller.CurrentsA, "durations_ms": controller.DurationsMs,
                      "thresholds_v": controller.VoltageThresholdsV}
_CONTROLLER_FIELDS = _fields(controller.ControllerParams, exclude=tuple(_CONTROLLER_TABLES))
_GROUP_KEYS = ("energy_mode", "capacitor", "controller") + tuple(_NODE_FIELDS)


def _is_kind(value: Any, kind: str) -> bool:
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "str":
        return isinstance(value, str)
    if kind == "table":
        return isinstance(value, dict)
    if kind == "list":
        return isinstance(value, list)
    raise AssertionError(kind)


_KIND_NAMES = {"bool": "true or false", "int": "an integer", "number": "a number", "str": "a string",
               "table": "a table", "list": "a list"}


class _Table:
    """One table of the file being read: hands out its values, checked, and refuses leftovers."""

    def __init__(self, data: Dict[str, Any], where: str, path: str):
        self._data = dict(data)
        self.where = where
        self.path = path

    def name(self, key: str) -> str:
        return f"{self.where}.{key}" if self.where else key

    def error(self, key: str, message: str) -> ValueError:
        return ValueError(f"{self.path}: {self.name(key)}: {message}")

    def has(self, key: str) -> bool:
        return key in self._data

    def take(self, key: str, kind: str, default: Any = _ABSENT, required: bool = False) -> Any:
        if key not in self._data:
            if required:
                raise ValueError(f"{self.path}: {self.name(key)} is required")
            return default
        value = self._data.pop(key)
        if not _is_kind(value, kind):
            raise self.error(key, f"must be {_KIND_NAMES[kind]}, got {value!r}")
        return value

    def table(self, key: str, required: bool = False) -> "_Table":
        return _Table(self.take(key, "table", {}, required), self.name(key), self.path)

    def fields(self, kinds: Mapping[str, str]) -> Dict[str, Any]:
        """The values of the keys in kinds that this table has."""
        out = {}
        for key, kind in kinds.items():
            value = self.take(key, kind)
            if value is not _ABSENT:
                out[key] = value
        return out

    def done(self) -> None:
        if self._data:
            keys = ", ".join(sorted(self._data))
            raise ValueError(f"{self.path}: {self.where or 'the top level'} has no key {keys}")


def _merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> Dict[str, Any]:
    """over on top of base, merging tables key by key."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _apply_override(data: Dict[str, Any], dotted: str, value: Any, path: str) -> None:
    """Set the value at a dotted key, such as reader.params.slots or tag_group.0.count, creating
    missing tables on the way."""
    keys = dotted.split(".")
    node: Any = data
    for i, key in enumerate(keys):
        last = i == len(keys) - 1
        where = ".".join(keys[:i + 1])
        if isinstance(node, list):
            if not key.isdigit() or int(key) >= len(node):
                raise ValueError(f"{path}: override {dotted}: {'.'.join(keys[:i])} has no entry {key}")
            if last:
                node[int(key)] = value
            else:
                node = node[int(key)]
        elif isinstance(node, dict):
            if last:
                node[key] = value
            else:
                node = node.setdefault(key, {})
        else:
            raise ValueError(f"{path}: override {dotted}: {where} is inside a value, not a table")


class _Placement(NamedTuple):
    node_id: int
    x: float
    y: float


class _Group(NamedTuple):
    energy_mode: str
    node_args: Dict[str, Any]
    cap_params: capacitor.CapacitorParams
    cap_args: Dict[str, Any]
    controller_params: controller.ControllerParams
    tags: List[_Placement]


class _TagParts(NamedTuple):
    node: radiodevices.Node
    downlink: Dict[str, Any]
    uplink: Dict[str, Any]
    coverage: propagation.CoverageMap
    group: _Group


class Scenario:
    """A scenario read from a TOML file, in the form amber.split.hosts runs.

    Attributes
    ----------
    SIM_TIME_MS : int
        Simulated duration, as passed to env.run(until=...).
    NODE_IDS : list of int
        The Tags, in order.
    BS_IDS : list of int
        The base stations, in order.
    SEED : int
        The seed of the run: the file's, or one drawn by load().
    seed_drawn : bool
        True when the file gives no seed and load() drew SEED.
    protocol : Protocol
        The protocol the base stations and Tags run.
    path : str
        The scenario file.
    """

    def __init__(self, path: str, data: Dict[str, Any]):
        self.path = path
        top = _Table(data, "", path)
        for key in data:
            if key not in _TOP_TABLES:
                raise ValueError(f"{path}: unknown table {key} (a scenario has {', '.join(_TOP_TABLES)})")

        run = top.table("run", required=True)
        self.SIM_TIME_MS: int = run.take("sim_time_ms", "int", required=True)
        if self.SIM_TIME_MS < 1:
            raise run.error("sim_time_ms", f"must be at least 1, got {self.SIM_TIME_MS}")
        seed = run.take("seed", "int", None)
        run.done()
        self.seed_drawn = seed is None
        self.SEED: int = random.SystemRandom().randrange(2 ** 32) if seed is None else seed

        self._base_stations = [self._read_base_station(t, i) for i, t in enumerate(self._array(top, "base_station"))]
        self.BS_IDS: List[int] = [bs["id"] for bs in self._base_stations]
        if len(set(self.BS_IDS)) != len(self.BS_IDS):
            raise ValueError(f"{path}: two base stations have the same id: {self.BS_IDS}")

        radio = top.table("radio")
        self._radio = radio.fields(_RADIO_FIELDS)
        radio.done()

        self._energy = self._read_energy(top.table("energy")) if top.has("energy") else None

        defaults = top.take("tag_defaults", "table", {})
        for key in defaults:
            if key in _PLACEMENT:
                raise ValueError(f"{path}: tag_defaults.{key}: a placement belongs in a tag_group")
        self._groups: List[_Group] = []
        self.NODE_IDS: List[int] = []
        for i, data_group in enumerate(self._array(top, "tag_group")):
            self._groups.append(self._read_group(_merge(defaults, data_group), i))

        reader = top.table("reader", required=True)
        name = reader.take("protocol", "str", required=True)
        params = reader.take("params", "table", {})
        self._reader_options = reader.fields(_READER_OPTIONS)
        reader.done()
        top.done()
        try:
            self.protocol = protocols.load(name, params, self.NODE_IDS)
        except ValueError as e:
            raise ValueError(f"{path}: reader: {e}") from None

    # ---------------- reading ----------------

    def _array(self, top: _Table, key: str) -> List[Dict[str, Any]]:
        """An array of tables, which a scenario must have at least one of."""
        tables = top.take(key, "list", required=True)
        if not tables or not all(isinstance(t, dict) for t in tables):
            raise top.error(key, f"must be one or more [[{key}]] tables")
        return tables

    def _read_base_station(self, data: Dict[str, Any], i: int) -> Dict[str, Any]:
        t = _Table(data, f"base_station[{i}]", self.path)
        bs = {"id": t.take("id", "int", required=True)}
        bs.update(t.fields(_BS_FIELDS))
        for key in ("x", "y"):
            if key not in bs:
                raise ValueError(f"{self.path}: {t.name(key)} is required")
        sectors = t.take("sectors", "list", [])
        bs["sectors"] = []
        for j, sector_data in enumerate(sectors):
            if not isinstance(sector_data, dict):
                raise t.error(f"sectors[{j}]", "must be a table")
            s = _Table(sector_data, f"{t.where}.sectors[{j}]", self.path)
            sector = s.fields(_SECTOR_FIELDS)
            for key in ("azimuth_deg", "beamwidth_deg"):
                if key not in sector:
                    raise ValueError(f"{self.path}: {s.name(key)} is required")
            s.done()
            bs["sectors"].append(sector)
        t.done()
        return bs

    def _read_energy(self, t: _Table) -> Dict[str, Any]:
        trace = t.take("trace", "str", required=True)
        out = {"file_path": os.path.join(os.path.dirname(os.path.abspath(self.path)), trace)}
        out.update(t.fields({"column": "str", "resistance": "number"}))
        t.done()
        if not os.path.isfile(out["file_path"]):
            raise t.error("trace", f"no file {out['file_path']}")
        return out

    def _read_group(self, data: Dict[str, Any], i: int) -> _Group:
        t = _Table(data, f"tag_group[{i}]", self.path)
        mode = t.take("energy_mode", "str", required=True)
        if mode not in _ENERGY_MODES:
            raise t.error("energy_mode", f"must be one of {', '.join(_ENERGY_MODES)}, got {mode!r}")
        if mode != "wpt" and self._energy is None:
            raise t.error("energy_mode", f"{mode} Tags need an [energy] table")
        node_args = t.fields(_NODE_FIELDS)

        cap = t.table("capacitor")
        cap_params = capacitor.CapacitorParams(**cap.fields(_CAP_PARAMS_FIELDS))
        cap_args = cap.fields(_CAP_FIELDS)
        cap.done()

        ctrl = t.table("controller", required=True)
        parts = {}
        for key, cls in _CONTROLLER_TABLES.items():
            sub = ctrl.table(key, required=True)
            kinds = _fields(cls)
            values = sub.fields(kinds)
            for field in kinds:
                if field not in values:
                    raise ValueError(f"{self.path}: {sub.name(field)} is required")
            sub.done()
            parts[key] = cls(**values)
        controller_params = controller.ControllerParams(**parts, **ctrl.fields(_CONTROLLER_FIELDS))
        ctrl.done()

        places = self._read_places(t, i)
        ids = t.take("ids", "list", None)
        if ids is None:
            first = max(self.NODE_IDS) + 1 if self.NODE_IDS else 0
            ids = list(range(first, first + len(places)))
        elif len(ids) != len(places) or not all(_is_kind(n, "int") for n in ids):
            raise t.error("ids", f"must be {len(places)} integers, one per Tag, got {ids!r}")
        for node_id in ids:
            if node_id in self.NODE_IDS or ids.count(node_id) > 1:
                raise t.error("ids", f"Tag id {node_id} is used twice")
        t.done()
        self.NODE_IDS.extend(ids)
        tags = [_Placement(node_id, x, y) for node_id, (x, y) in zip(ids, places)]
        return _Group(mode, node_args, cap_params, cap_args, controller_params, tags)

    def _read_places(self, t: _Table, i: int) -> List[Tuple[float, float]]:
        if t.has("positions"):
            if t.has("count") or t.has("layout"):
                raise t.error("positions", "give either positions or count and layout, not both")
            positions = t.take("positions", "list")
            if not positions or not all(isinstance(p, list) and len(p) == 2 and all(_is_kind(v, "number") for v in p)
                                        for p in positions):
                raise t.error("positions", f"must be a list of one or more [x, y], got {positions!r}")
            return [(x, y) for x, y in positions]
        count = t.take("count", "int", required=True)
        if count < 1:
            raise t.error("count", f"must be at least 1, got {count}")
        layout = t.table("layout", required=True)
        kind = layout.take("kind", "str", required=True)
        if kind != "ring":
            raise layout.error("kind", f"must be ring, got {kind!r}")
        r_min = layout.take("r_min", "number", required=True)
        r_max = layout.take("r_max", "number", required=True)
        if not 0 <= r_min <= r_max:
            raise layout.error("r_min", f"must be at least 0 and at most r_max, got {r_min} and {r_max}")
        seed = layout.take("seed", "int", None)
        center = layout.take("center", "list", None)
        if center is not None and not (len(center) == 2 and all(_is_kind(v, "number") for v in center)):
            raise layout.error("center", f"must be [x, y], got {center!r}")
        layout.done()
        rng = random.Random(f"{self.SEED}/layout/{i}" if seed is None else seed)
        places = []
        for _ in range(count):
            r = rng.uniform(r_min, r_max)
            ang = rng.uniform(0, 2 * math.pi)
            x, y = r * math.cos(ang), r * math.sin(ang)
            if center is not None:
                x, y = x + center[0], y + center[1]
            places.append((x, y))
        return places

    # ---------------- building, as amber.split.hosts calls it ----------------

    def _make_base_station(self, bs: Dict[str, Any]) -> radiodevices.BaseStation:
        args = {k: v for k, v in bs.items() if k != "sectors"}
        return radiodevices.BaseStation(sectors=[radiodevices.Sector(**s) for s in bs["sectors"]], **args)

    def build_shared(self, env) -> Dict[int, _TagParts]:
        """The objects every Tag shares: the energy source and one coverage map per Tag group."""
        energy_source = None
        if self._energy is not None:
            energy_source = energy.EnvEnergySource(env=env, **self._energy)
        bss = [self._make_base_station(bs) for bs in self._base_stations]
        shared = {}
        for group in self._groups:
            nodes = [radiodevices.Node(id=t.node_id, x=t.x, y=t.y, **group.node_args) for t in group.tags]
            power_fn = None if energy_source is None else (lambda node: energy_source.ext_power)
            cov = propagation.CoverageMap(base_stations=bss, nodes=nodes, node_energy_mode=group.energy_mode,
                                          node_ext_power_fn=power_fn, **self._radio)
            dl = cov.compute_bs_to_point(nodes)
            cov.calculate_node_power(nodes, dl)
            ul = cov.compute_point_to_bs(nodes)
            for node in nodes:
                shared[node.id] = _TagParts(node, dl, ul, cov, group)
        return shared

    def build_tag(self, env, shared: Dict[int, _TagParts], node_id: int):
        """One Tag's Capacitor, BackscatterModule (the protocol's TAG_CLASS) and Controller, drawing
        from the Tag's own random generator; returns the BackscatterModule."""
        node, dl, ul, cov, group = shared[node_id]
        rng = random.Random(f"{self.SEED}/{node_id}")
        cap = capacitor.Capacitor(env=env, id=node.id, params=group.cap_params, **group.cap_args)
        bsm = self.protocol.TAG_CLASS(env=env, node=node, bs_processes=[], uplink_results=ul, downlink_results=dl,
                                      rng=rng)
        controller.Controller(env=env, capacitor_ctrl=cap, node=node, params=group.controller_params, backscatter=bsm,
                              coverage_map=cov, downlink_results=dl, rng=rng)
        self.protocol.setup_tag(bsm)
        return bsm

    def build_reader(self, env, tags) -> List[bsengine.BSBehavior]:
        """One BSBehavior per base station, running the protocol, given the Tags as backscatter_modules."""
        out = []
        for bs in self._base_stations:
            behavior = bsengine.BSBehavior(env=env, base_station=self._make_base_station(bs), backscatter_modules=tags,
                                           **self.protocol.reader_args(), **self._reader_options)
            self.protocol.setup_reader(behavior)
            out.append(behavior)
        return out


def load(path: str, overrides: Optional[Mapping[str, Any]] = None) -> Scenario:
    """Read a scenario file.

    Parameters
    ----------
    path : str
        The TOML file.
    overrides : Mapping[str, Any], optional
        {dotted key: value} set in the file before it is read, such as {"run.seed": 7}.

    Returns
    -------
    Scenario
        The scenario, ready for amber.split.hosts.

    Raises
    ------
    ValueError
        If the file is not valid TOML or not a valid scenario; the message names the key.
    """
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"{path}: {e}") from None
    for dotted, value in (overrides or {}).items():
        _apply_override(data, dotted, value, path)
    return Scenario(path, data)
