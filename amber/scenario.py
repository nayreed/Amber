# amber/scenario.py
"""Scenarios as TOML files: the values of a simulation, kept apart from the protocol it runs.

A scenario file says what is simulated (base stations, Tags, radio, energy, duration, seed) and
names the protocol that drives it (see amber.protocols). load() reads one into a Scenario, which
runs on one machine (python -m amber.run) or as a Reader container and one container per Tag
(amber-reader and amber-tag in OAI), through amber.hosts and amber.iq.

Every key is named as the field of the Amber class it sets, and every value that a file leaves
out keeps that class's default. demo_experiments/<protocol>/*.toml are complete examples. The
tables:

```text
[run]
    sim_time_ms     simulated duration in ms (required)
    seed            integer from which every random draw comes, each Tag's from its own
                    generator; without one, load() draws a new seed, so every run is a new sample
[reader]
    protocol        the protocol (required): one of the A-IoT MAC library's (amber.protocols.names())
    params          table of the protocol's parameters
    enable_sic      the Reader cancels each decoded D2R and looks again (default true)
    noise_figure_db the Reader's receiver noise figure (default 6)
    cw_leak_dbm     the Reader's carrier leaking into its receiver (default -38, as 38.194's
                    REFSENS assumes; see amber.link.CarrierLeak)
    phase_noise_floor_dbc_hz, phase_noise_corner_hz
                    the leak's phase noise: a floor (default -103) rising as 1/f^2 below the
                    corner (default 5300 Hz), calibrated to 38.194 REFSENS
[[base_station]]    one or more: id, x, y (required) and BaseStation's other fields, and sectors,
                    a list of tables of Sector's fields (azimuth_deg and beamwidth_deg required)
[radio]             CoverageMap's options: freq_hz, pathloss_model, los, extra_np_per_m,
                    bandwidth_hz, noise_figure_db, combine_mode
[energy]            the energy trace (EnvEnergySource), needed by "external" and "hybrid" Tags:
                    trace (path relative to the scenario file, required), column, resistance
[phy]               the Rel-19 A-IoT link (amber.link.LinkConfig): m_chip_symb, n_rb,
                    t_bit_tau, r_sfs, x, r_code, r_block, l_amble, interval_cp, i_add, d2r_tbs,
                    modulation, timing_error, tag_snr_db; all required. rfsim carries real
                    waveforms (see amber.iq) and the physics decides what is received. Needs
                    exactly one base station.
[network]           the network the A-IoT link runs in, in the terms of the four topologies of
                    the Rel-19 A-IoT study (see Network); without it, a standalone Reader:
    cell            the 5G cell that shares the air, by name ("n28-25prb"); absent: no 5G
    r2d             who sends R2D: "reader" (default), "gnb" or "ue"
    d2r             who receives D2R: "reader" (default), "gnb" or "ue"
    cw              who sends the carrier the Tags reflect: "reader" (default), "gnb-dl",
                    "gnb-ul", "ue-ul", "cw-node-dl" or "cw-node-ul"
    relay           the UE forwards what it reads to the gNB over 5G (default false)
    hosts           table: the machine each node of a 5G room runs on, by address, for OAI's
                    amber-compose, which writes one compose file per machine. Keys: hub (the
                    rfsim_hub, required with any other), gnb, ue, cw (a CW node), reader (a
                    standalone Reader) and tags; a node not named runs with the hub. Absent: one
                    machine. amber.run ignores it.
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

from . import backscatter, capacitor, controller, energy, propagation, protocols, radiodevices
from .link import LinkConfig

_ABSENT = object()

# The Reader's options a scenario may set.
_READER_OPTIONS = {"enable_sic": "bool", "noise_figure_db": "number", "cw_leak_dbm": "number",
                   "phase_noise_floor_dbc_hz": "number", "phase_noise_corner_hz": "number"}
_ENERGY_MODES = ("wpt", "external", "hybrid")
_TOP_TABLES = ("run", "reader", "base_station", "radio", "energy", "phy", "network", "tag_defaults", "tag_group")
_CELLS = ("n28-25prb",)
_R2D_D2R = ("reader", "gnb", "ue")
# The [network] carriers that come from a node of their own, outside the Reader (TR 38.769 6.8.1).
CW_NODES = ("cw-node-dl", "cw-node-ul")
_CW = ("reader", "gnb-dl", "gnb-ul", "ue-ul") + CW_NODES
# The nodes of a 5G room that [network.hosts] places on a machine.
HOST_NODES = ("hub", "gnb", "ue", "cw", "reader", "tags")


class Network(NamedTuple):
    """Where the A-IoT link runs: which node sends R2D, which receives D2R, which sends the carrier
    wave (CW), and the 5G cell sharing the air. The four topologies of the Rel-19 study (CW options
    after TR 38.769) are:

        1  r2d = d2r = "gnb": the gNB is the Reader
        2  r2d = d2r = "ue", relay = true: the UE reads the Tags and forwards over 5G
        3  r2d = "ue", d2r = "gnb" (downlink assistance) or r2d = "gnb", d2r = "ue" (uplink assistance)
        4  r2d = d2r = "ue", relay = false: the UE reads the Tags for itself

    With the defaults a standalone Reader sends R2D and the carrier and receives D2R. amber.run
    simulates the A-IoT link only; OAI's amber-compose builds the rest."""
    cell: Optional[str] = None
    r2d: str = "reader"
    d2r: str = "reader"
    cw: str = "reader"
    relay: bool = False
    hosts: Tuple[Tuple[str, str], ...] = ()  # (node, address), in HOST_NODES order; () is one machine

    def host(self, node: str) -> Optional[str]:
        """The address of the machine node (one of HOST_NODES) runs on, or None on one machine."""
        placed = dict(self.hosts)
        return placed.get(node, placed.get("hub"))

    @property
    def topology(self) -> Optional[int]:
        """The study's topology number, or None for a standalone Reader."""
        ends = {self.r2d, self.d2r}
        if ends == {"reader"}:
            return None
        if ends == {"gnb"}:
            return 1
        if ends == {"ue"}:
            return 2 if self.relay else 4
        return 3
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
_LINK_FIELDS = _fields(LinkConfig, exclude=("r_sfs",))
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
    """A scenario read from a TOML file, in the form amber.hosts and amber.iq run.

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
    phy : LinkConfig
        The [phy] table.
    enable_sic : bool
        Whether the Reader cancels each decoded D2R and looks again.
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

        self.network: Network = self._read_network(top.table("network"))

        t = top.table("phy", required=True)
        values = {key: t.take(key, kind, required=True) for key, kind in _LINK_FIELDS.items()}
        values["r_sfs"] = tuple(t.take("r_sfs", "list", required=True))
        t.done()
        try:
            self.phy: LinkConfig = LinkConfig(**values)
        except ValueError as e:
            raise ValueError(f"{path}: [phy]: {e}") from None
        if len(self._base_stations) != 1:
            raise ValueError(f"{path}: [phy] needs exactly one base station, got {len(self._base_stations)}")

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
            self.protocol = protocols.load(name, params, self.NODE_IDS, self.phy)
        except ValueError as e:
            raise ValueError(f"{path}: reader: {e}") from None

    # ---------------- reading ----------------

    def _read_network(self, t: _Table) -> Network:
        def choice(key: str, allowed: Sequence[str], default: Optional[str]) -> Optional[str]:
            value = t.take(key, "str", default)
            if value is not None and value not in allowed:
                raise t.error(key, f"must be one of {', '.join(map(repr, allowed))}, got {value!r}")
            return value

        net = Network(cell=choice("cell", _CELLS, None), r2d=choice("r2d", _R2D_D2R, "reader"),
                      d2r=choice("d2r", _R2D_D2R, "reader"), cw=choice("cw", _CW, "reader"),
                      relay=t.take("relay", "bool", False))
        hosts_table = t.table("hosts")
        net = net._replace(hosts=self._read_hosts(hosts_table, net))
        t.done()
        if ("reader" in (net.r2d, net.d2r)) and net.r2d != net.d2r:
            raise t.error("r2d", "a standalone Reader both sends R2D and receives D2R")
        if (net.cw == "reader") != (net.r2d == "reader"):
            raise t.error("cw", '"reader" is the standalone Reader\'s own carrier: it goes with r2d = d2r = "reader"')
        if net.cell is None and any(n.split("-")[0] in ("gnb", "ue") for n in (net.r2d, net.d2r, net.cw)):
            raise t.error("cell", "a gNB or a UE in the A-IoT link needs the 5G cell it belongs to")
        if net.relay and (net.r2d, net.d2r) != ("ue", "ue"):
            raise t.error("relay", 'only a UE that reads the Tags (r2d = d2r = "ue") relays')
        return net

    @staticmethod
    def _read_hosts(t: _Table, net: Network) -> Tuple[Tuple[str, str], ...]:
        """[network.hosts]: (node, address) of each node it names, which must be in the room."""
        hosts = []
        for node in HOST_NODES:
            address = t.take(node, "str", None)
            if address is None:
                continue
            if not address or any(c.isspace() for c in address):
                raise t.error(node, f"must be a host name or an address, got {address!r}")
            hosts.append((node, address))
        t.done()
        if not hosts:
            return ()
        nodes = dict(hosts)
        if net.cell is None:
            raise t.error(hosts[0][0], "only a 5G room (network.cell) runs on several machines")
        if "hub" not in nodes:
            raise t.error("hub", "is required: every other node connects to the rfsim_hub")
        in_room = {"hub", "gnb", "ue", "tags"}
        if net.cw in CW_NODES:
            in_room.add("cw")
        if net.r2d == "reader":
            in_room.add("reader")
        for node in nodes:
            if node not in in_room:
                raise t.error(node, f"this room has no {node} node")
        return tuple(hosts)

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

    # ---------------- building, as amber.hosts and amber.iq call it ----------------

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

    def iq_links(self) -> Dict[int, Tuple[int, int, float]]:
        """IQ mode: {node id: (index of the sector that hears it best, its subcarrier_shift, the
        power that sector receives from it in dBm)}. The sector is what a Reader with one receiver
        per sector knows of where a frame came from."""
        bss = [self._make_base_station(bs) for bs in self._base_stations]
        out = {}
        for group in self._groups:
            nodes = [radiodevices.Node(id=t.node_id, x=t.x, y=t.y, **group.node_args) for t in group.tags]
            cov = propagation.CoverageMap(base_stations=bss, nodes=nodes, node_energy_mode=group.energy_mode,
                                          node_ext_power_fn=None, **self._radio)
            cov.calculate_node_power(nodes, cov.compute_bs_to_point(nodes))
            per_sector = cov.compute_point_to_bs(nodes).get("per_sector_powers", {})
            for node in nodes:
                heard = [(powers[node.id], name) for name, powers in per_sector.items() if node.id in powers]
                best = max(heard) if heard else (-999.0, "")
                sector = int(best[1].split("_S")[1]) if heard else -1
                out[node.id] = (sector, int(getattr(node, "subcarrier_shift", 0) or 0), float(best[0]))
        return out

    @property
    def reader_noise_figure_db(self) -> float:
        return float(self._reader_options.get("noise_figure_db", 6.0))

    @property
    def cw_leak(self):
        """(leak dBm, phase noise floor dBc/Hz, corner Hz) of the Reader's carrier leak."""
        from .link import CW_LEAK_DBM, PHASE_NOISE_CORNER_HZ, PHASE_NOISE_FLOOR_DBC_HZ
        o = self._reader_options
        return (float(o.get("cw_leak_dbm", CW_LEAK_DBM)),
                float(o.get("phase_noise_floor_dbc_hz", PHASE_NOISE_FLOOR_DBC_HZ)),
                float(o.get("phase_noise_corner_hz", PHASE_NOISE_CORNER_HZ)))

    @property
    def enable_sic(self) -> bool:
        return bool(self._reader_options.get("enable_sic", True))

    def build_tag(self, env, shared: Dict[int, _TagParts], node_id: int):
        """One Tag's Capacitor, AiotTag and Controller, drawing from the Tag's own random generator;
        returns the AiotTag."""
        node, dl, ul, cov, group = shared[node_id]
        rng = random.Random(f"{self.SEED}/{node_id}")
        cap = capacitor.Capacitor(env=env, id=node.id, params=group.cap_params, **group.cap_args)
        bsm = backscatter.AiotTag(env=env, node=node, uplink_results=ul, downlink_results=dl)
        bsm.controller = controller.Controller(env=env, capacitor_ctrl=cap, node=node, params=group.controller_params,
                                               backscatter=bsm, coverage_map=cov, downlink_results=dl, rng=rng)
        return bsm


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
        The scenario, ready for amber.iq.

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
