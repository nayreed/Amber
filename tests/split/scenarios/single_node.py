# tests/split/scenarios/single_node.py
"""One node going through Amber's built-in protocol (send_id, ack, send_data)."""

import os
import sys

from amber import bsengine, radiodevices

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo_common  # noqa: E402

SIM_TIME_MS = 5_001
NODE_IDS = [0]
SEED = 2026
BS_IDS = [0]
CAPACITANCE = 200e-6
SCHEDULE = ([("tx", 5, "cmd", {"target": -1, "cmd": "send_id"})] + [("rx", 10, "listen") for _ in range(2)]
            + [("tx", 5, "ack_slot", {"cmd": "ack"}), ("tx", 5, "cmd", {"target": -1, "cmd": "send_data"})]
            + [("rx", 10, "listen") for _ in range(2)])


def build_shared(env):
    node = radiodevices.Node(id=0, x=5, y=20, height=1.5, sensitivity_dbm=-100, efficiency=0.7)
    return demo_common.build_shared(env, demo_common.three_sector_bs(), [("hybrid", [node])])


def build_tag(env, shared, node_id):
    return demo_common.build_node(env, shared, node_id, CAPACITANCE, SEED)


def build_reader(env, tags):
    return [bsengine.BSBehavior(env=env, base_station=demo_common.three_sector_bs(), schedule=SCHEDULE,
                                backscatter_modules=tags, loop=True, enable_sic=True)]
