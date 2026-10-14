# tests/split/scenarios/registration.py
"""Amber's built-in protocol from the start: unregistered nodes answer send_id in a random slot,
are acked, then answer send_data. Exercises acks and the Tag state machine, with SIC."""

import os
import sys

from amber import bsengine, radiodevices

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo_common  # noqa: E402

SIM_TIME_MS = 8_001
NODE_IDS = [0, 1, 2, 3]
BS_IDS = [0]
SEED = 12345
CAPACITANCE = 200e-6
SCHEDULE = ([("tx", 5, "cmd", {"target": -1, "cmd": "send_id"})] + [("rx", 10, "listen") for _ in range(3)]
            + [("tx", 5, "ack_slot", {"cmd": "ack"}), ("tx", 5, "cmd", {"target": -1, "cmd": "send_data"})]
            + [("rx", 10, "listen") for _ in range(2)])


def build_shared(env):
    nodes = [radiodevices.Node(id=i, x=x, y=y, height=1.5, sensitivity_dbm=-100, efficiency=0.7)
             for i, (x, y) in zip(NODE_IDS, ((0, 15), (10, 25), (-12, 30), (5, 40)))]
    return demo_common.build_shared(env, demo_common.three_sector_bs(), [("hybrid", nodes)])


def build_tag(env, shared, node_id):
    return demo_common.build_node(env, shared, node_id, CAPACITANCE, SEED)


def build_reader(env, tags):
    return [bsengine.BSBehavior(env=env, base_station=demo_common.three_sector_bs(), schedule=SCHEDULE,
                                backscatter_modules=tags, loop=True, enable_sic=True)]
