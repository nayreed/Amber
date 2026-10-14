# tests/split/scenarios/unicast.py
"""Amber's unicast demo: each registered node is polled in turn with send_data, one slot each."""

import os
import sys

from amber import bsengine, radiodevices

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo_common  # noqa: E402

SIM_TIME_MS = 8_001
NODE_IDS = [0, 1, 2]
BS_IDS = [0]
CAPACITANCE = 300e-6
SCHEDULE = [entry for node_id in NODE_IDS
            for entry in (("tx", 5, "cmd", {"target": node_id, "cmd": "send_data"}), ("rx", 10, "listen"))]


def build_shared(env):
    nodes = [radiodevices.Node(id=i, x=0, y=y, height=1.5, sensitivity_dbm=-100, efficiency=0.7)
             for i, y in zip(NODE_IDS, (20, 30, 100))]
    return demo_common.build_shared(env, demo_common.three_sector_bs(), [("wpt", nodes[:2]), ("hybrid", nodes[2:])])


def build_tag(env, shared, node_id):
    bsm = demo_common.build_node(env, shared, node_id, CAPACITANCE)
    bsm.state = "registered"
    return bsm


def build_reader(env, tags):
    bs = bsengine.BSBehavior(env=env, base_station=demo_common.three_sector_bs(), schedule=SCHEDULE,
                             backscatter_modules=tags, loop=True)
    bs.nodes_registered = list(NODE_IDS)
    return [bs]
