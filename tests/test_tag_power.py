# tests/test_tag_power.py
"""Checks that a Tag hears and answers the Reader only while its controller is on, and that the Tags
send data they sensed."""

import os
import sys
import tempfile
import types
import unittest

import numpy as np
import simpy

from amber import iq, scenario
from amber.backscatter import AiotTag
from amber.hosts import TagHost

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import SCENARIO_DIR, SHORT_MS  # noqa: E402


def tag():
    return AiotTag(env=simpy.Environment(), node=types.SimpleNamespace(id=1))


class PoweredTest(unittest.TestCase):
    def test_a_tag_without_a_controller_senses_nothing_and_is_registered(self):
        bsm = tag()
        self.assertEqual((bsm.sensed_value(), bsm.state, bsm.powered), (0, "registered", True))
        self.assertIsNone(bsm.controller_tx_ready(100))

    def test_the_tag_records_what_it_hears(self):
        bsm = tag()
        bsm.heard_sdu(b"\x01\x02")
        self.assertEqual([(r.end_ms, r.sdu_hex) for r in bsm.rx_records], [(0.0, "0102")])

    def test_the_controller_says_when_the_tag_is_on(self):
        s = scenario.load(os.path.join(SCENARIO_DIR, "registration.toml"))
        hosts = [TagHost(s, node_id) for node_id in s.NODE_IDS]
        seen_on = set()
        for ms in range(1, s.SIM_TIME_MS, 50):
            for host in hosts:
                host.deliver(ms)
                host.step(ms)
                if host.bsm.powered:
                    seen_on.add(host.node_id)
        self.assertEqual(seen_on, set(s.NODE_IDS))

    def test_an_unpowered_tag_does_not_reflect(self):
        s = scenario.load(os.path.join(SCENARIO_DIR, "single_node.toml"), {"run.sim_time_ms": SHORT_MS})
        with tempfile.TemporaryDirectory() as out:
            tag_session = iq.IqTagSession(s, 0, iq.BLOCK_BYTES, out)
            carrier = np.ones(iq.AIR_SAMPLES, dtype=complex)
            reflected = []
            for ms in range(200):    # the capacitor has not charged yet
                tag_session.host.deliver(ms)
                tag_session._step(ms, carrier)
                reflected.append(tag_session.link.powered)
            self.assertFalse(any(reflected))
            self.assertEqual(tag_session.link.sent, [])
            self.assertEqual(tag_session.bsm.tx_records, [])


class SensedDataTest(unittest.TestCase):
    def test_a_tag_sends_the_last_value_its_controller_processed(self):
        bsm = tag()
        bsm.controller = types.SimpleNamespace(processed_data=137, is_active=False)
        self.assertEqual(bsm.sensed_value(), 137)
        send = types.SimpleNamespace(msg="data", params=types.SimpleNamespace(r_sfs=2), slot=1)
        bsm.started_d2r(40, send)
        self.assertEqual([(r.start_ms, r.kind, r.payload, r.r_sfs, r.slot) for r in bsm.tx_records],
                         [(40.0, "data", 137, 2, 1)])
        self.assertEqual(iq.read_sdu(iq.make_sdu(5, bsm.sensed_value())), (5, 137))

    def test_a_starting_d2r_puts_a_controller_waiting_to_send_in_its_transmitting_state(self):
        bsm = tag()
        bsm.controller = types.SimpleNamespace(processed_data=0, is_active=True, state_name="wait_slot", counter=4)
        send = types.SimpleNamespace(msg="msg1", params=types.SimpleNamespace(r_sfs=1), slot=0)
        send.message = types.SimpleNamespace(random_id=777)
        bsm.started_d2r(3, send)
        self.assertEqual((bsm.controller.state_name, bsm.controller.counter), ("transmitting", 0))
        self.assertEqual(bsm.tx_records[0].payload, 777)

    def test_a_starting_d2r_leaves_a_controller_that_is_still_sensing_alone(self):
        # Restarting the cycle at every answer would stop a Tag ever sensing: it would send 0.
        bsm = tag()
        bsm.controller = types.SimpleNamespace(processed_data=0, is_active=True, state_name="sensing", counter=4)
        send = types.SimpleNamespace(msg="data", params=types.SimpleNamespace(r_sfs=1), slot=0)
        bsm.started_d2r(3, send)
        self.assertEqual((bsm.controller.state_name, bsm.controller.counter), ("sensing", 4))

    def test_what_the_reader_decodes_is_what_the_tags_sent(self):
        s = scenario.load(os.path.join(SCENARIO_DIR, "adaptive_aloha.toml"), {"run.sim_time_ms": SHORT_MS})
        with tempfile.TemporaryDirectory() as out:
            reader, tags = iq.run(s, out)
        decoded = {(r[3], r[4]) for r in reader.record.rx if r[2] == "data" and r[3] != -1}
        self.assertGreater(len(decoded), 0)
        sent = {(t.node_id, r.payload) for t in tags for r in t.bsm.tx_records if r.kind != "msg1"}
        self.assertLessEqual(decoded, sent)
        self.assertTrue(all(0 <= value <= 255 for _, value in decoded))


if __name__ == "__main__":
    unittest.main()
