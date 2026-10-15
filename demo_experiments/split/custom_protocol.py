# demo_experiments/split/custom_protocol.py
"""Amber's custom_protocol demo (demo_experiments/custom_protocol), adaptive framed-slotted ALOHA, as
an amber.split scenario with its settings unchanged. The node layout is drawn as the demo draws it,
from random.seed(7), so the nodes stand where they stand in the demo; each node then picks its
slots from its own random generator instead of Python's global one.

LAYOUT_SEED places the nodes and SEED seeds the run, so a run with another SEED keeps the layout."""

import math
import os
import random
import sys

from amber import backscatter, bsengine, radiodevices

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _demo  # noqa: E402

SIM_TIME_MS = 15_001
N_NODES = 12
NODE_IDS = list(range(N_NODES))
BS_IDS = [0]
SEED = 7
LAYOUT_SEED = 7
CAPACITANCE = 300e-6
NODE_MIN_R = 5.0
NODE_MAX_R = 40.0
START_SLOTS = 20
MIN_SLOTS = 2
MAX_SLOTS = 32
SECTORS = [(0, 65, -100), (120, 65, -100), (240, 65, -100)]  # (azimuth_deg, beamwidth_deg, sensitivity_dbm)


class AdaptiveAlohaNode(backscatter.BackscatterModule):
    def handle_command(self, cmd, bs_id, data):
        if cmd == "collect":
            self.chosen_slot_idx = self.rng.randint(0, len(self.rx_slots) - 1) if self.rx_slots else -1
            self.state = "active"
            self.last_tx_command_time = self.env.now

    def build_tx_payload(self, sensor_data):
        if self.state == "active":
            return {"type": "data", "payload": sensor_data}
        return None


def adaptive_aloha_policy(bs):
    n_slots = START_SLOTS
    prev_collided = 0
    load_ema = 0.0
    bs.adapt_history = []
    while True:
        yield [("tx", 5, "collect", {"cmd": "collect", "target": -1})] + [("rx", 8, "slot") for _ in range(n_slots)]
        heard = len(bs.decoded_this_frame)
        collided = bs.total_collided - prev_collided
        prev_collided = bs.total_collided
        bs.adapt_history.append((bs.env.now / 1000.0, n_slots, heard, collided))
        load_ema = 0.75 * load_ema + 0.25 * (heard + collided)
        target = min(MAX_SLOTS, max(MIN_SLOTS, round(load_ema * 2)))
        if n_slots < target:
            n_slots += 1
        elif n_slots > target + 1:
            n_slots -= 1


def build_shared(env):
    layout = random.Random(LAYOUT_SEED)
    nodes = []
    for i in NODE_IDS:
        r = layout.uniform(NODE_MIN_R, NODE_MAX_R)
        ang = layout.uniform(0, 2 * math.pi)
        nodes.append(radiodevices.Node(id=i, x=r * math.cos(ang), y=r * math.sin(ang), height=1.5, sensitivity_dbm=-100,
                                       efficiency=0.7))
    return _demo.demo_common.build_shared(env, _demo.make_bs(SECTORS), [("hybrid", nodes)])


def build_tag(env, shared, node_id):
    return _demo.demo_common.build_node(env, shared, node_id, CAPACITANCE, SEED, module_cls=AdaptiveAlohaNode)


def build_reader(env, tags):
    return [bsengine.BSBehavior(env=env, base_station=_demo.make_bs(SECTORS), policy=adaptive_aloha_policy,
                                backscatter_modules=tags, enable_sic=True)]
