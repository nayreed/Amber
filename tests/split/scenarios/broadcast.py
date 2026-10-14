# tests/split/scenarios/broadcast.py
"""Amber's broadcast demo: three registered nodes answer a broadcast send_data in one of two slots,
no SIC."""

import os
import sys

from amber import bsengine, radiodevices

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo_common  # noqa: E402

SIM_TIME_MS = 8_001
NODE_IDS = [0, 1, 2]
BS_IDS = [0]
SEED = 12345
CAPACITANCE = 300e-6
SCHEDULE = [("tx", 5, "cmd", {"target": -1, "cmd": "send_data"})] + [("rx", 10, "listen") for _ in range(2)]


def _bs():
    return demo_common.three_sector_bs()


def build_shared(env):
    nodes = [radiodevices.Node(id=i, x=0, y=y, height=1.5, sensitivity_dbm=-100, efficiency=0.7)
             for i, y in zip(NODE_IDS, (20, 30, 100))]
    return demo_common.build_shared(env, _bs(), [("wpt", nodes[:2]), ("hybrid", nodes[2:])])


def build_tag(env, shared, node_id):
    bsm = demo_common.build_node(env, shared, node_id, CAPACITANCE, SEED)
    bsm.state = "registered"
    return bsm


def build_reader(env, tags):
    bs = bsengine.BSBehavior(env=env, base_station=_bs(), schedule=SCHEDULE, backscatter_modules=tags, loop=True,
                             enable_sic=False)
    bs.nodes_registered = list(NODE_IDS)
    return [bs]
