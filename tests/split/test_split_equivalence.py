# tests/split/test_split_equivalence.py
"""Checks that a scenario split into a ReaderHost and one TagHost per node, exchanging framed
messages every millisecond, produces exactly what one-process Amber produces.

All hosts run in this process and share Python's global random generator, as one-process Amber's
nodes do. Each millisecond the harness hands out commands to every Tag before stepping any of
them, which is the order one-process Amber consumes random numbers in, so the comparison is exact
even for protocols that draw random slots.
"""

import os
import random
import tempfile
import unittest

import simpy

from amber.split import wire
from amber.split.hosts import ReaderHost, TagHost, build_one_process, load_scenario
from amber.split.session import ReaderSession, TagSession

SCENARIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")
BLOCK_BYTES = 8192
SEED = 12345


def run_one_process(path):
    scenario = load_scenario(path)
    random.seed(SEED)
    env = simpy.Environment()
    bss, bsms = build_one_process(scenario, env)
    env.run(until=scenario.SIM_TIME_MS)
    return bss, bsms


def run_split(path):
    random.seed(SEED)
    reader = ReaderHost(load_scenario(path))
    # Same construction order as build_one_process(), so construction-time random draws match.
    tags = []
    for node_id in reader.scenario.NODE_IDS:
        tags.append(TagHost(load_scenario(path), node_id))
    for ms in range(reader.scenario.SIM_TIME_MS):
        down = wire.frame({"cmds": wire.encode_deliveries(reader.run_ms(ms))}, BLOCK_BYTES)
        for tag in tags:
            tag.deliver(ms, wire.decode_deliveries(wire.unframe(down)["cmds"], tag.node_id))
        uplinks = []
        for tag in tags:
            up = wire.frame({"up": wire.encode_uplinks(tag.step(ms))}, BLOCK_BYTES)
            uplinks += wire.decode_uplinks(wire.unframe(up)["up"], tag.node_id)
        reader.apply_uplinks(ms, uplinks)
    return reader.bss, [tag.bsm for tag in tags]


class SplitEquivalenceTest(unittest.TestCase):
    def check(self, name):
        path = os.path.join(SCENARIO_DIR, name + ".py")
        ref_bss, ref_bsms = run_one_process(path)
        bss, bsms = run_split(path)

        self.assertGreater(sum(len(bs.rx_packets) for bs in ref_bss), 0, "scenario too short to exercise uplinks")
        for ref, got in zip(ref_bss, bss):
            self.assertEqual(ref.rx_packets, got.rx_packets)
            self.assertEqual(ref.tx_packets, got.tx_packets)
            self.assertEqual(ref.nodes_registered, got.nodes_registered)
            self.assertEqual(ref.nodes_pending_ack, got.nodes_pending_ack)
            self.assertEqual(getattr(ref, "adapt_history", None), getattr(got, "adapt_history", None))
        for ref, got in zip(ref_bsms, bsms):
            self.assertEqual(ref.tx_records, got.tx_records)
            self.assertEqual(ref.rx_records, got.rx_records)
            self.assertEqual(ref.state, got.state)
            self.assertEqual(ref.acks_received, got.acks_received)

    def test_broadcast(self):
        self.check("broadcast")

    def test_unicast(self):
        self.check("unicast")

    def test_registration(self):
        self.check("registration")

    def test_adaptive_aloha(self):
        self.check("adaptive_aloha")

    def test_single_node(self):
        self.check("single_node")


class SessionTest(unittest.TestCase):
    """The block protocol amber-reader and amber-tag run, without rfsim: same result as one process."""

    def test_reader_and_tag_sessions(self):
        path = os.path.join(SCENARIO_DIR, "single_node.py")
        ref_bss, ref_bsms = run_one_process(path)
        random.seed(SEED)
        with tempfile.TemporaryDirectory() as out:
            reader = ReaderSession(path, BLOCK_BYTES, out)
            tag = TagSession(path, 0, BLOCK_BYTES, out)
            # A few idle blocks first, as when the Tag connects after the Reader started.
            self.assertFalse(reader.on_block(bytes(BLOCK_BYTES)))
            done = False
            while not done:
                answer, tag_done = tag.reply(reader.next_block())
                done = reader.on_block(answer)
                self.assertEqual(done, tag_done)
            reader.finish()
            tag.finish()
            self.assertEqual(ref_bss[0].rx_packets, reader.host.bss[0].rx_packets)
            self.assertEqual(ref_bsms[0].tx_records, tag.host.bsm.tx_records)
            with open(os.path.join(out, "reader_bs0_rx.csv")) as f:
                self.assertEqual(len(f.readlines()), len(ref_bss[0].rx_packets) + 1)
            self.assertTrue(os.path.exists(os.path.join(out, "tag0_tx.csv")))

    def test_reader_refuses_several_tags(self):
        with self.assertRaises(ValueError):
            ReaderSession(os.path.join(SCENARIO_DIR, "broadcast.py"), BLOCK_BYTES, ".")


class WireTest(unittest.TestCase):
    def test_round_trip_and_grouping(self):
        from amber.split.proxies import Delivery, Uplink
        data = {"cmd": "send_id", "target": -1, "rx_slots": [(10, 20), (20, 30)]}
        deliveries = [Delivery(0, "send_id", 0, data), Delivery(1, "send_id", 0, data), Delivery(1, "ack", 0, {})]
        encoded = wire.unframe(wire.frame({"c": wire.encode_deliveries(deliveries)}, 512))["c"]
        self.assertEqual(len(encoded), 2)
        self.assertEqual(wire.decode_deliveries(encoded, 1), deliveries[1:])
        self.assertEqual(wire.decode_deliveries(encoded, 2), [])
        ups = [Uplink(0, 3, {"x": 1}, -71.123456789012345, 2, 1)]
        self.assertEqual(wire.decode_uplinks(wire.unframe(wire.frame({"u": wire.encode_uplinks(ups)}, 512))["u"], 3), ups)

    def test_empty_block_and_overflow(self):
        self.assertIsNone(wire.unframe(bytes(64)))
        with self.assertRaises(ValueError):
            wire.frame({"x": "y" * 100}, 64)


if __name__ == "__main__":
    unittest.main()
