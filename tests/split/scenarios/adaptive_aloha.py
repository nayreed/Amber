# tests/split/scenarios/adaptive_aloha.py
"""Amber's custom_protocol demo: adaptive framed-slotted ALOHA driven by a BSBehavior policy, twelve
nodes, SIC on. Node positions come from a local RNG so every host builds the same layout."""

import math
import os
import random
import sys

from amber import backscatter, bsengine, radiodevices

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo_common  # noqa: E402

SIM_TIME_MS = 6_001
N_NODES = 12
NODE_IDS = list(range(N_NODES))
BS_IDS = [0]
CAPACITANCE = 300e-6
NODE_MIN_R = 5.0
NODE_MAX_R = 40.0
START_SLOTS = 20
MIN_SLOTS = 2
MAX_SLOTS = 32
LAYOUT_SEED = 7


class AdaptiveAlohaNode(backscatter.BackscatterModule):
    def handle_command(self, cmd, bs_id, data):
        if cmd == "collect":
            self.chosen_slot_idx = random.randint(0, len(self.rx_slots) - 1) if self.rx_slots else -1
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
        bs.adapt_history.append((bs.env.now, n_slots, heard, collided))
        load_ema = 0.75 * load_ema + 0.25 * (heard + collided)
        target = min(MAX_SLOTS, max(MIN_SLOTS, round(load_ema * 2)))
        if n_slots < target:
            n_slots += 1
        elif n_slots > target + 1:
            n_slots -= 1


def build_shared(env):
    rng = random.Random(LAYOUT_SEED)
    nodes = []
    for i in NODE_IDS:
        r = rng.uniform(NODE_MIN_R, NODE_MAX_R)
        ang = rng.uniform(0, 2 * math.pi)
        nodes.append(radiodevices.Node(id=i, x=r * math.cos(ang), y=r * math.sin(ang), height=1.5, sensitivity_dbm=-100,
                                       efficiency=0.7))
    return demo_common.build_shared(env, demo_common.three_sector_bs(), [("hybrid", nodes)])


def build_tag(env, shared, node_id):
    return demo_common.build_node(env, shared, node_id, CAPACITANCE, module_cls=AdaptiveAlohaNode)


def build_reader(env, tags):
    return [bsengine.BSBehavior(env=env, base_station=demo_common.three_sector_bs(), policy=adaptive_aloha_policy,
                                backscatter_modules=tags, enable_sic=True)]
