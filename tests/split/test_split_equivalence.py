# tests/split/test_split_equivalence.py
"""Checks that a scenario split into a ReaderHost and one TagHost per node, exchanging framed
messages every millisecond, produces exactly what build_one_process() produces: the whole scenario
in one environment, with the same event order.

Every node in the scenarios draws from its own random generator. The split runs reseed Python's
global generator before every Tag step and step the Tags in reverse order, as separate processes
would leave it in any state, so a node that still drew from it would make the comparison fail.
"""

import os
import random
import tempfile
import unittest

from amber import scenario
from amber.split import reference, wire
from amber.split.hosts import ReaderHost, TagHost, build_one_process, load_scenario
from amber.split.session import ReaderSession, TagSession

SCENARIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")
DEMO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "demo_experiments")
BLOCK_BYTES = 8192
SEED = 12345


def load(path):
    """The scenario, with SEED when the file has none (the demos' own files), so the Reader and every
    Tag draw the same numbers."""
    s = scenario.load(path)
    return scenario.load(path, {"run.seed": SEED}) if s.seed_drawn else s


def run_one_process(path, loader=None):
    s = (loader or load)(path)
    random.seed(SEED)
    env, bss, bsms = build_one_process(s)
    env.run(until=s.SIM_TIME_MS)
    return bss, bsms


def scramble_global_random(node_id, ms):
    """Leave Python's global generator as an unrelated process might have it."""
    random.seed(f"other process {node_id} {ms}")


def message_bytes(block):
    """How many bytes of a framed block the message uses (length prefix included)."""
    return 4 + int.from_bytes(block[:4], "little")


def run_split(path, loader=None, sizes=None):
    """The scenario split into a Reader and one host per Tag. If sizes is a dict, it gets the largest
    message each way, as {"reader": bytes, "tag": bytes}."""
    loader = loader or load
    reader = ReaderHost(loader(path))
    tags = []
    for node_id in reversed(reader.scenario.NODE_IDS):
        scramble_global_random(node_id, -1)
        tags.append(TagHost(loader(path), node_id))
    for ms in range(reader.scenario.SIM_TIME_MS):
        down = wire.frame({"cmds": wire.encode_deliveries(reader.run_ms(ms))}, BLOCK_BYTES)
        ups = {}
        for tag in tags:
            scramble_global_random(tag.node_id, ms)
            tag.deliver(ms, wire.decode_deliveries(wire.unframe(down)["cmds"], tag.node_id))
            ups[tag.node_id] = wire.frame({"up": wire.encode_uplinks(tag.step(ms))}, BLOCK_BYTES)
            if sizes is not None:
                sizes["tag"] = max(sizes.get("tag", 0), message_bytes(ups[tag.node_id]))
        if sizes is not None:
            sizes["reader"] = max(sizes.get("reader", 0), message_bytes(down))
        uplinks = []
        for node_id in reader.scenario.NODE_IDS:
            uplinks += wire.decode_uplinks(wire.unframe(ups[node_id])["up"], node_id)
        reader.apply_uplinks(ms, uplinks)
    return reader.bss, sorted((tag.bsm for tag in tags), key=lambda bsm: bsm.node.id)


class SplitEquivalenceTest(unittest.TestCase):
    def check(self, path):
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

    def test_scenarios(self):
        for name in ("broadcast", "unicast", "registration", "adaptive_aloha", "single_node", "gen2_q"):
            with self.subTest(name):
                self.check(os.path.join(SCENARIO_DIR, name + ".toml"))

    def test_demos(self):
        for name in ("broadcast", "broadcast_sic", "unicast", "custom_protocol"):
            with self.subTest(name):
                self.check(os.path.join(DEMO_DIR, name, name + ".toml"))


class SessionTest(unittest.TestCase):
    """The block protocol amber-reader and amber-tag run, without rfsim: same result as one process."""

    def run_sessions(self, name, out):
        path = os.path.join(SCENARIO_DIR, name + ".toml")
        reader = ReaderSession(path, BLOCK_BYTES, out)
        tags = [TagSession(path, node_id, BLOCK_BYTES, out) for node_id in load_scenario(path).NODE_IDS]
        # An empty block first, as from a Tag that has just connected.
        self.assertFalse(reader.on_blocks([bytes(BLOCK_BYTES)]))
        done = False
        while not done:
            block = reader.next_block()
            answers, tags_done = [], []
            # The Reader gets the Tags' blocks in whatever order rfsim has their connections.
            for tag in reversed(tags):
                scramble_global_random(tag.node_id, reader.host.env.now)
                answer, tag_done = tag.reply(block)
                answers.append(answer)
                tags_done.append(tag_done)
            done = reader.on_blocks(answers)
            self.assertEqual(tags_done, [done] * len(tags))
        reader.finish()
        for tag in tags:
            tag.finish()
        return reader, tags

    def check(self, name):
        ref_bss, ref_bsms = run_one_process(os.path.join(SCENARIO_DIR, name + ".toml"))
        with tempfile.TemporaryDirectory() as out:
            reader, tags = self.run_sessions(name, out)
            self.assertEqual(ref_bss[0].rx_packets, reader.host.bss[0].rx_packets)
            self.assertEqual(ref_bss[0].tx_packets, reader.host.bss[0].tx_packets)
            for ref, tag in zip(ref_bsms, tags):
                self.assertEqual(ref.tx_records, tag.host.bsm.tx_records)
                self.assertEqual(ref.rx_records, tag.host.bsm.rx_records)
            with open(os.path.join(out, "reader_bs0_rx.csv")) as f:
                self.assertEqual(len(f.readlines()), len(ref_bss[0].rx_packets) + 1)
            for tag in tags:
                self.assertTrue(os.path.exists(os.path.join(out, f"tag{tag.node_id}_tx.csv")))

    def test_single_node(self):
        self.check("single_node")

    def test_registration_four_tags(self):
        self.check("registration")

    def test_adaptive_aloha_twelve_tags(self):
        self.check("adaptive_aloha")

    def test_reference_writes_the_same_files(self):
        with tempfile.TemporaryDirectory() as split_out, tempfile.TemporaryDirectory() as ref_out:
            self.run_sessions("registration", split_out)
            reference.run(os.path.join(SCENARIO_DIR, "registration.toml"), ref_out)
            names = sorted(os.listdir(split_out))
            self.assertEqual(names, sorted(os.listdir(ref_out)))
            self.assertIn("tag3_tx.csv", names)
            for name in names:
                with open(os.path.join(split_out, name)) as a, open(os.path.join(ref_out, name)) as b:
                    self.assertEqual(a.read(), b.read(), name)

    def started_reader(self):
        """A Reader of the 4-node registration scenario that has heard every hello."""
        path = os.path.join(SCENARIO_DIR, "registration.toml")
        reader = ReaderSession(path, BLOCK_BYTES, ".")
        tags = [TagSession(path, node_id, BLOCK_BYTES, ".") for node_id in load_scenario(path).NODE_IDS]
        idle = reader.next_block()
        self.assertFalse(reader.on_blocks([tag.reply(idle)[0] for tag in tags]))
        return reader, tags

    def test_missing_tag_is_an_error(self):
        reader, tags = self.started_reader()
        block = reader.next_block()
        with self.assertRaisesRegex(RuntimeError, "no answer"):
            reader.on_blocks([tag.reply(block)[0] for tag in tags[1:]])

    def test_two_tags_with_one_node_id_are_an_error(self):
        reader, tags = self.started_reader()
        block = reader.next_block()
        answers = [tag.reply(block)[0] for tag in tags]
        with self.assertRaisesRegex(RuntimeError, "two Tags"):
            reader.on_blocks(answers + answers[:1])

    def test_unknown_node_is_an_error(self):
        path = os.path.join(SCENARIO_DIR, "registration.toml")
        reader = ReaderSession(path, BLOCK_BYTES, ".")
        stranger = wire.frame({"t": "hello", "node": 99}, BLOCK_BYTES)
        with self.assertRaisesRegex(ValueError, "not in the scenario"):
            reader.on_blocks([stranger])


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
