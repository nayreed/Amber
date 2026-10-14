# demo_experiments/split/broadcast_sic.py
"""Amber's broadcast_sic demo (demo_experiments/broadcast_sic) as an amber.split scenario, with its settings unchanged.
Each node draws from its own random generator instead of Python's global one."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _demo  # noqa: E402

SIM_TIME_MS = 20_001
NODE_IDS = _demo.NODE_IDS
BS_IDS = _demo.BS_IDS
SEED = 12345
CAPACITANCE = 200e-6
SECTORS = [(0, 30, -80), (120, 65, -100), (240, 65, -100)]  # (azimuth_deg, beamwidth_deg, sensitivity_dbm)
ENABLE_SIC = True
TX_DURATION_MS = 5
RX_DURATION_MS = 15
NUM_BROADCAST_SLOTS = 3
SCHEDULE = [("tx", TX_DURATION_MS, "cmd", {"target": -1, "cmd": "send_data"})] + \
    [("rx", RX_DURATION_MS, "listen") for _ in range(NUM_BROADCAST_SLOTS)]


def build_shared(env):
    return _demo.build_shared(env, SECTORS)


def build_tag(env, shared, node_id):
    return _demo.build_tag(env, shared, node_id, CAPACITANCE, SEED)


def build_reader(env, tags):
    return _demo.build_reader(env, tags, SECTORS, SCHEDULE, ENABLE_SIC)
