# demo_experiments/split/_demo.py
"""What the broadcast, broadcast_sic and unicast scenarios share: the demos' three nodes (WPT, WPT,
Hybrid) and their base station, built with tests/split/scenarios/demo_common."""

import os
import sys

from amber import bsengine, radiodevices

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tests", "split", "scenarios"))
import demo_common  # noqa: E402

NODE_IDS = [0, 1, 2]
BS_IDS = [0]


def make_bs(sectors):
    """The demos' base station; sectors: (azimuth_deg, beamwidth_deg, sensitivity_dbm) for each sector."""
    return radiodevices.BaseStation(
        id=0, x=0, y=0, site_radius=2,
        sectors=[radiodevices.Sector(azimuth_deg=a, beamwidth_deg=b, power=46, antenna_gain_dbi=15, sensitivity_dbm=s,
                                     height=25)
                 for a, b, s in sectors])


def build_shared(env, sectors):
    nodes = [radiodevices.Node(id=i, x=0, y=y, height=1.5, sensitivity_dbm=-100, efficiency=0.7)
             for i, y in zip(NODE_IDS, (20, 30, 100))]
    return demo_common.build_shared(env, make_bs(sectors), [("wpt", nodes[:2]), ("hybrid", nodes[2:])])


def build_tag(env, shared, node_id, capacitance, seed):
    bsm = demo_common.build_node(env, shared, node_id, capacitance, seed)
    bsm.state = "registered"
    return bsm


def build_reader(env, tags, sectors, schedule, enable_sic):
    bs = bsengine.BSBehavior(env=env, base_station=make_bs(sectors),
                             schedule=schedule, backscatter_modules=tags, loop=True, enable_sic=enable_sic)
    bs.nodes_registered = list(NODE_IDS)
    return [bs]
