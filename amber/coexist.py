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

room_powers() gives what each receiver of a 5G room gets from each sender, in dBm; OAI's
amber-compose turns them into the gains of the rfsim_hub links.
"""

import math
from dataclasses import dataclass
from typing import Any, Dict, NamedTuple, Optional, Tuple

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


class TagPowers(NamedTuple):
    """What one Tag gets and sends, in dBm: the carrier from the Reader (Amber's own model), the
    gNB's DL, its reflection at the Reader (as amber.iq has it) and at the gNB."""
    carrier: float
    gnb: float
    at_reader: float
    at_gnb: float


class RoomPowers(NamedTuple):
    """What each receiver of a 5G room with a standalone Reader gets from each sender, in dBm."""
    gnb_at_ue: float
    ue_at_gnb: float
    reader_at_ue: float
    reader_at_gnb: float
    ue_at_reader: float
    tags: Dict[int, TagPowers]


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


def room_powers(scenario: Any) -> Optional[RoomPowers]:
    """The powers of a scenario's 5G room with a standalone Reader whose [network] places the gNB
    and the UE; None when it does not."""
    net = scenario.network
    if net.gnb is None or net.ue is None:
        return None
    fc = scenario.freq_hz
    los = scenario.los
    gnb, ue = net.gnb, net.ue
    g_site, u_site = (gnb.x, gnb.y, gnb.height), (ue.x, ue.y, ue.height)

    def loss(a, sa, b, sb):
        return pair_loss_db(a, sa, b, sb, fc, los, ue.indoor)

    pl_gu = loss("gnb", g_site, "ue", u_site)
    gnb_at_ue = gnb.power_dbm + gnb.antenna_gain_dbi + ue.antenna_gain_dbi - pl_gu
    ue_at_gnb = ue.power_dbm + ue.antenna_gain_dbi + gnb.antenna_gain_dbi - pl_gu

    bs = scenario.reader_base_station()
    eirp_ue, h_ue_side = _reader_to(bs, ue.x, ue.y, True)
    g_ue_side, _ = _reader_to(bs, ue.x, ue.y, False)
    eirp_gnb, h_gnb_side = _reader_to(bs, gnb.x, gnb.y, True)
    r_ue, r_gnb = (bs.x, bs.y, h_ue_side), (bs.x, bs.y, h_gnb_side)
    reader_at_ue = eirp_ue + ue.antenna_gain_dbi - loss("reader", r_ue, "ue", u_site)
    reader_at_gnb = eirp_gnb + gnb.antenna_gain_dbi - loss("reader", r_gnb, "gnb", g_site)
    ue_at_reader = ue.power_dbm + ue.antenna_gain_dbi + g_ue_side - loss("ue", u_site, "reader", r_ue)

    tags = {}
    for node_id, (node, carrier_w, backscatter_dbm, at_reader_dbm) in scenario.tag_radio().items():
        t_site = (node.x, node.y, float(node.height))
        aerr = propagation.angular_error_deg(propagation.compass_to_math(float(node.azimuth_deg or 0.0)),
                                             propagation.bearing_deg(node.x, node.y, gnb.x, gnb.y))
        g_tag = 10 * math.log10(float(propagation.antenna_from_node(node).gain_linear(aerr)))
        pl = loss("gnb", g_site, "tag", t_site)
        tags[node_id] = TagPowers(
            carrier=float(propagation.watts_to_dbm(carrier_w)) if carrier_w > 0 else -999.0,
            gnb=gnb.power_dbm + gnb.antenna_gain_dbi + g_tag - pl,
            at_reader=at_reader_dbm,
            at_gnb=backscatter_dbm + g_tag + gnb.antenna_gain_dbi - pl)
    return RoomPowers(gnb_at_ue, ue_at_gnb, reader_at_ue, reader_at_gnb, ue_at_reader, tags)
