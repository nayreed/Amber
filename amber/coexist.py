# amber/coexist.py
"""How a 5G cell and the A-IoT link hear each other, from where their nodes are.

The coexistence study of TR 38.769 (Table 6.6.2.1-1, deployment D1T1: the Reader and its devices
indoors, the NR base station outdoors) gives every pair of nodes a TR 38.901 path loss model:

    NR BS - NR UE                   UMa
    outdoor NR UE - Reader/device   UMi-Street Canyon
    indoor NR UE - device           InH-Office
    indoor NR UE - Reader           InF-DH
    device - Reader                 InF-DH

PL = PLb + PLtw + PLin + N(0, sigma), with the high O2I penetration loss of TR 38.901 for a pair
with one end indoors, and a minimum coupling loss of 70 dB to the base station. Here:

- the table does not name the pair NR BS - Reader/device; it takes UMa with the O2I loss, as for an
  indoor UE;
- PLin (0.5 dB per metre indoors, to the building's wall) and the shadow fading are left out: a
  room has no walls, and the hub's links are fixed;
- LOS or NLOS is one choice for every pair ([radio] los of the scenario), not drawn per pair;
- the pair device - Reader stays with the scenario's own model ([radio]), which amber.iq applies.

Room gives what each node of a 5G room gets from each other, in dBm, in every topology; OAI's
amber-compose turns that into the gains of the rfsim_hub links.
"""

import math
from dataclasses import dataclass
from typing import Any, Tuple

from . import propagation, radiodevices

MCL_DB = 70.0  # minimum coupling loss to the NR base station (TR 38.769 Table 6.6.2.1-1)


@dataclass(frozen=True)
class Site:
    """Where a node of the 5G cell is and what it sends: position (m) in the scenario's plane,
    antenna height (m), total power (dBm) and antenna gain (dBi, the same in every direction)."""
    x: float
    y: float
    height: float
    power_dbm: float
    antenna_gain_dbi: float
    indoor: bool = False


# The defaults of TR 38.769 Tables 6.6.2.2-1 (macro NR BS) and 6.6.2.2-3 (UE).
GNB_SITE = {"height": 25.0, "power_dbm": 46.0, "antenna_gain_dbi": 15.0}
UE_SITE = {"height": 1.5, "power_dbm": 23.0, "antenna_gain_dbi": 0.0}


def _d3d(d2d: float, h_a: float, h_b: float) -> float:
    return max(math.hypot(d2d, h_a - h_b), 1.0)


def uma_db(d2d: float, fc_hz: float, h_bs: float, h_ut: float, los: bool) -> float:
    """TR 38.901 Table 7.4.1-1, UMa."""
    return float(propagation.urban_macro_loss_db_38_901(d2d, fc_hz, h_bs=h_bs, h_ut=h_ut, los=los))


def umi_db(d2d: float, fc_hz: float, h_bs: float, h_ut: float, los: bool) -> float:
    """TR 38.901 Table 7.4.1-1, UMi-Street Canyon (h_E = 1 m)."""
    fc = fc_hz / 1e9
    d2d = max(d2d, 1.0)
    d3d = _d3d(d2d, h_bs, h_ut)
    d_bp = 4.0 * (h_bs - 1.0) * max(h_ut - 1.0, 0.1) * fc_hz / propagation.C
    if d2d <= d_bp:
        pl_los = 32.4 + 21.0 * math.log10(d3d) + 20.0 * math.log10(fc)
    else:
        pl_los = (32.4 + 40.0 * math.log10(d3d) + 20.0 * math.log10(fc)
                  - 9.5 * math.log10(d_bp ** 2 + (h_bs - h_ut) ** 2))
    if los:
        return pl_los
    return max(pl_los, 35.3 * math.log10(d3d) + 22.4 + 21.3 * math.log10(fc) - 0.3 * (h_ut - 1.5))


def inh_office_db(d2d: float, fc_hz: float, h_a: float, h_b: float, los: bool) -> float:
    """TR 38.901 Table 7.4.1-1, InH-Office."""
    fc = fc_hz / 1e9
    d3d = _d3d(d2d, h_a, h_b)
    pl_los = 32.4 + 17.3 * math.log10(d3d) + 20.0 * math.log10(fc)
    if los:
        return pl_los
    return max(pl_los, 38.3 * math.log10(d3d) + 17.30 + 24.9 * math.log10(fc))


def inf_dh_db(d2d: float, fc_hz: float, h_a: float, h_b: float, los: bool) -> float:
    """TR 38.901 Table 7.4.1-1, InF-DH (dense clutter, high base station)."""
    fc = fc_hz / 1e9
    d3d = _d3d(d2d, h_a, h_b)
    pl_los = 31.84 + 21.50 * math.log10(d3d) + 19.00 * math.log10(fc)
    if los:
        return pl_los
    return max(pl_los, 33.63 + 21.9 * math.log10(d3d) + 20.0 * math.log10(fc))


def o2i_high_db(fc_hz: float) -> float:
    """TR 38.901 Table 7.4.3-2, the high-loss model's PLtw (IRR glass and concrete), without PLin
    and the spread."""
    fc = fc_hz / 1e9
    glass = 23.0 + 0.3 * fc
    concrete = 5.0 + 4.0 * fc
    return 5.0 - 10.0 * math.log10(0.7 * 10 ** (-glass / 10) + 0.3 * 10 ** (-concrete / 10))


# The ends of a pair: "gnb", "ue", "reader" or "tag".
_AIOT = ("reader", "tag")


def pair_loss_db(a: str, site_a: Tuple[float, float, float], b: str, site_b: Tuple[float, float, float],
                 fc_hz: float, los: bool, ue_indoor: bool) -> float:
    """The path loss (dB) between node a at site_a and node b at site_b, each (x, y, height), as
    TR 38.769 Table 6.6.2.1-1 models that pair (see the module's notes)."""
    ends = {a, b}
    d2d = math.hypot(site_a[0] - site_b[0], site_a[1] - site_b[1])
    by = {a: site_a, b: site_b}
    if "gnb" in ends:
        other = (ends - {"gnb"}).pop()
        pl = uma_db(d2d, fc_hz, by["gnb"][2], by[other][2], los)
        if other in _AIOT or ue_indoor:
            pl += o2i_high_db(fc_hz)
        return max(pl, MCL_DB)
    if ends == {"ue", "tag"} or ends == {"ue", "reader"}:
        other = (ends - {"ue"}).pop()
        if not ue_indoor:
            h_hi, h_lo = max(by["ue"][2], by[other][2]), min(by["ue"][2], by[other][2])
            return umi_db(d2d, fc_hz, h_hi, h_lo, los) + o2i_high_db(fc_hz)
        if other == "tag":
            return inh_office_db(d2d, fc_hz, by["ue"][2], by[other][2], los)
        return inf_dh_db(d2d, fc_hz, by["ue"][2], by[other][2], los)
    if ends == {"reader", "tag"}:
        return inf_dh_db(d2d, fc_hz, site_a[2], site_b[2], los)
    raise ValueError(f"no path loss model for the pair {a} - {b}")


def _reader_to(bs: radiodevices.BaseStation, x: float, y: float, power: bool) -> Tuple[float, float]:
    """(dBm the Reader's best sector sends toward (x, y) plus its gain there, or that gain alone
    without power; that sector's height)."""
    best = None
    for sec in bs.sectors:
        aerr = propagation.angular_error_deg(propagation.compass_to_math(sec.azimuth_deg),
                                             propagation.bearing_deg(bs.x, bs.y, x, y))
        g_dbi = 10 * math.log10(float(propagation.antenna_from_sector(sec).gain_linear(aerr)))
        value = g_dbi + (float(sec.power) if power else 0.0)
        if best is None or value > best[0]:
            best = (value, float(sec.height))
    return best


# Where a node of the room is and what it sends and hears toward another: (pair model kind, (x, y,
# height), EIRP dBm, receive antenna gain dBi).
_End = Tuple[str, Tuple[float, float, float], float, float]


class Room:
    """The nodes of a 5G room where the scenario places them, and the power each gets from each
    other, in dBm: the gNB ("gnb"), the UE ("ue"), the base station ("bs": Amber's Reader, or the
    gNB or the UE that is the Reader, and the CW node, all at the scenario's [[base_station]]) and
    the Tags (their node ids).

    Between the base station and a Tag, the powers are amber.iq's own (the scenario's [radio]
    model): the carrier a Tag gets, and what the base station gets of its reflection. Every other
    pair has the model of TR 38.769 Table 6.6.2.1-1 (pair_loss_db). The 5G node that is the Reader
    takes the base station's place, its best sector toward the other end, and is indoors, as the
    study's Reader is; a placed UE is indoors as its table says. A CW node at the base station is
    of the same kind as the node that stands there, so it reaches the other 5G node through the
    same path as that node does."""

    def __init__(self, scenario: Any):
        net = scenario.network
        self.fc_hz = scenario.freq_hz
        self.los = scenario.los
        self.bs = scenario.reader_base_station()
        self.reader_node = net.reader_node
        self.sites = {"gnb": net.gnb, "ue": net.ue}
        for node, site in self.sites.items():
            if site is None and node != self.reader_node:
                raise ValueError(f"{scenario.path}: network.{node}: x and y are required in this room: "
                                 "the gains between the 5G cell and the A-IoT link come from where its nodes are")
        self.ue_indoor = net.ue.indoor if net.ue is not None else True
        self.tags = scenario.tag_radio()

    def at_base_station(self, node: Any) -> bool:
        return node == "bs" or node == self.reader_node

    def _end(self, node: Any, toward: Tuple[float, float]) -> _End:
        if isinstance(node, int):
            tag, _, reflected_dbm, _ = self.tags[node]
            aerr = propagation.angular_error_deg(propagation.compass_to_math(float(tag.azimuth_deg or 0.0)),
                                                 propagation.bearing_deg(tag.x, tag.y, *toward))
            g_tag = 10 * math.log10(float(propagation.antenna_from_node(tag).gain_linear(aerr)))
            return "tag", (tag.x, tag.y, float(tag.height)), reflected_dbm + g_tag, g_tag
        if self.at_base_station(node):
            eirp, height = _reader_to(self.bs, *toward, True)
            gain, _ = _reader_to(self.bs, *toward, False)
            # the base station is the Reader node's place, and a CW node there has its kind
            kind = (self.reader_node or "reader") if node == "bs" else node
            return kind, (self.bs.x, self.bs.y, height), eirp, gain
        site = self.sites[node]
        return node, (site.x, site.y, site.height), site.power_dbm + site.antenna_gain_dbi, site.antenna_gain_dbi

    def _xy(self, node: Any) -> Tuple[float, float]:
        if isinstance(node, int):
            tag = self.tags[node][0]
            return tag.x, tag.y
        if self.at_base_station(node):
            return self.bs.x, self.bs.y
        return self.sites[node].x, self.sites[node].y

    def power(self, tx: Any, rx: Any) -> float:
        """What rx gets from tx, in dBm: tx's EIRP toward rx, rx's antenna gain toward tx, and the
        pair's path loss. Two nodes at the base station are one place, which has no path loss."""
        if self.at_base_station(tx) and self.at_base_station(rx):
            raise ValueError(f"{tx} and {rx} both stand at the base station")
        if self.at_base_station(tx) and isinstance(rx, int):
            carrier_w = self.tags[rx][1]
            return float(propagation.watts_to_dbm(carrier_w)) if carrier_w > 0 else -999.0
        if isinstance(tx, int) and self.at_base_station(rx):
            return self.tags[tx][3]
        kind_tx, site_tx, eirp, _ = self._end(tx, self._xy(rx))
        kind_rx, site_rx, _, gain = self._end(rx, self._xy(tx))
        return eirp + gain - pair_loss_db(kind_tx, site_tx, kind_rx, site_rx, self.fc_hz, self.los, self.ue_indoor)
