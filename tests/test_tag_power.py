# tests/test_tag_power.py
"""Checks that a Tag hears commands only while its controller is on, and that adaptive_aloha's Tags
send data they sensed."""

import os
import tempfile
import types
import unittest

import simpy

from amber import iq, scenario
from amber.backscatter import BackscatterModule
from amber.hosts import TagHost

SCENARIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")


def tag(env):
    node = types.SimpleNamespace(id=1)
    return BackscatterModule(env=env, node=node, bs_processes=[])


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
        hosts = [TagHost(s, node_id) for node_id in s.NODE_IDS]
        self.assertTrue(all(host.bsm.powered is False for host in hosts))  # off until the capacitor charges
        seen_on = set()
        for ms in range(1, s.SIM_TIME_MS, 50):
            for host in hosts:
                host.env.run(until=ms)
                if host.bsm.powered:
                    seen_on.add(host.node_id)
        self.assertEqual(seen_on, set(s.NODE_IDS))


class AdaptiveAlohaDataTest(unittest.TestCase):
    def test_tags_send_sensed_data(self):
        s = scenario.load(os.path.join(SCENARIO_DIR, "adaptive_aloha.toml"))
        s.SIM_TIME_MS = 3001
        with tempfile.TemporaryDirectory() as out:
            reader, _ = iq.run(s, out)
        decoded = [p for p in reader.host.bss[0].rx_packets if not p.collided]
        self.assertGreater(len(decoded), 0)
        self.assertTrue(all(100 <= p.payload <= 255 for p in decoded))


if __name__ == "__main__":
    unittest.main()
