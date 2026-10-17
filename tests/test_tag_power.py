# tests/test_tag_power.py
"""Checks that a Tag hears commands only while its controller is on, and that adaptive_aloha's Tags
send data they sensed."""

import os
import types
import unittest

import simpy

from amber import scenario
from amber.backscatter import BackscatterModule
from amber.hosts import build

SCENARIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")


def tag(env):
    node = types.SimpleNamespace(id=1, sensitivity_dbm=-100.0)
    return BackscatterModule(env=env, node=node, bs_processes=[], downlink_results={"best_pw_dbm": {1: -50.0}})


class PoweredTest(unittest.TestCase):
    def test_a_tag_without_a_controller_hears_commands(self):
        bsm = tag(simpy.Environment())
        bsm.receive_command("send_id", 0, {"rx_slots": [(10, 20)]})
        self.assertEqual((len(bsm.rx_records), bsm.state, bsm.rx_slots), (1, "wait_ack", [(10, 20)]))

    def test_an_unpowered_tag_ignores_commands(self):
        bsm = tag(simpy.Environment())
        bsm.state, bsm.rx_slots, bsm.chosen_slot_idx = "wait_ack", [(5, 15)], 0
        bsm.powered = False
        for cmd in ("send_id", "ack", "send_data"):
            bsm.receive_command(cmd, 0, {"rx_slots": [(30, 40)]})
        self.assertEqual((bsm.rx_records, bsm.state, bsm.rx_slots, bsm.chosen_slot_idx), ([], "wait_ack", [(5, 15)], 0))

    def test_the_controller_says_when_the_tag_is_on(self):
        s = scenario.load(os.path.join(SCENARIO_DIR, "registration.toml"))
        env, _, bsms = build(s)
        self.assertTrue(all(bsm.powered is False for bsm in bsms))  # off until the capacitor charges
        seen_on = set()
        for ms in range(1, s.SIM_TIME_MS, 50):
            env.run(until=ms)
            seen_on.update(bsm.node.id for bsm in bsms if bsm.powered)
        self.assertEqual(seen_on, {bsm.node.id for bsm in bsms})


class AdaptiveAlohaDataTest(unittest.TestCase):
    def test_tags_send_sensed_data(self):
        s = scenario.load(os.path.join(SCENARIO_DIR, "adaptive_aloha.toml"))
        env, bss, bsms = build(s)
        env.run(until=s.SIM_TIME_MS)
        decoded = [p for p in bss[0].rx_packets if not p.collided]
        self.assertGreater(len(decoded), 0)
        self.assertTrue(all(100 <= p.payload <= 255 for p in decoded))


if __name__ == "__main__":
    unittest.main()
