# demo_experiments/split/unicast.py
"""Amber's unicast demo (demo_experiments/unicast) as an amber.split scenario, with its settings unchanged.
Each node draws from its own random generator instead of Python's global one."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _demo  # noqa: E402

SIM_TIME_MS = 20_001
NODE_IDS = _demo.NODE_IDS
BS_IDS = _demo.BS_IDS
SEED = 12345
CAPACITANCE = 300e-6
SECTORS = [(0, 65, -100), (120, 65, -100), (240, 30, -100)]  # (azimuth_deg, beamwidth_deg, sensitivity_dbm)
ENABLE_SIC = True
TX_DURATION_MS = 5
RX_DURATION_MS = 10
SCHEDULE = [step for node_id in NODE_IDS
            for step in (("tx", TX_DURATION_MS, "cmd", {"target": node_id, "cmd": "send_data"}),
                         ("rx", RX_DURATION_MS, "listen"))]


def build_shared(env):
    return _demo.build_shared(env, SECTORS)


def build_tag(env, shared, node_id):
    return _demo.build_tag(env, shared, node_id, CAPACITANCE, SEED)


def build_reader(env, tags):
    return _demo.build_reader(env, tags, SECTORS, SCHEDULE, ENABLE_SIC)
