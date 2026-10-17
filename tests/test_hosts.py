# tests/test_hosts.py
"""Checks that a scenario run as a ReaderHost and one TagHost per node, exchanging framed messages
every millisecond as the OAI containers do, produces exactly what build() produces: the whole
scenario on one environment, with the same event order.

Every node in the scenarios draws from its own random generator. run_hosts() reseeds Python's
global generator before every Tag step and steps the Tags in reverse order, as separate containers
would leave it in any state, so a node that still drew from it would make the comparison fail.
"""

import contextlib
import glob
import io
import os
import random
import tempfile
import unittest

from amber import run as amber_run
from amber import scenario, wire
from amber.hosts import ReaderHost, TagHost, build, load_scenario
from amber.session import ReaderSession, TagSession

SCENARIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")
DEMO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "demo_experiments")
BLOCK_BYTES = 8192
SEED = 12345


def load(path):
    """The scenario, with SEED when the file has none (the demos' own files), so the Reader and every
    Tag draw the same numbers."""
    s = scenario.load(path)
    return scenario.load(path, {"run.seed": SEED}) if s.seed_drawn else s


def run_on_one_machine(path, loader=None):
    s = (loader or load)(path)
    random.seed(SEED)
    env, bss, bsms = build(s)
    env.run(until=s.SIM_TIME_MS)
    return bss, bsms


def scramble_global_random(node_id, ms):
    """Leave Python's global generator as an unrelated container might have it."""
    random.seed(f"other container {node_id} {ms}")


def run_hosts(path, loader=None):
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
        uplinks = []
        for node_id in reader.scenario.NODE_IDS:
            uplinks += wire.decode_uplinks(wire.unframe(ups[node_id])["up"], node_id)
        reader.apply_uplinks(ms, uplinks)
    return reader.bss, sorted((tag.bsm for tag in tags), key=lambda bsm: bsm.node.id)


class HostsTest(unittest.TestCase):
    def check(self, path):
        ref_bss, ref_bsms = run_on_one_machine(path)
        bss, bsms = run_hosts(path)

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
        for path in sorted(glob.glob(os.path.join(DEMO_DIR, "*", "*.toml"))):
            with self.subTest(os.path.relpath(path, DEMO_DIR)):
                self.check(path)


class SessionTest(unittest.TestCase):
    """The block protocol amber-reader and amber-tag run, without rfsim: same result as one machine."""

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
        ref_bss, ref_bsms = run_on_one_machine(os.path.join(SCENARIO_DIR, name + ".toml"))
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
                for side in ("tx", "rx"):
                    self.assertTrue(os.path.exists(os.path.join(out, f"tag{tag.node_id}_{side}.csv")))

    def test_single_node(self):
        self.check("single_node")

    def test_registration_four_tags(self):
        self.check("registration")

    def test_adaptive_aloha_twelve_tags(self):
        self.check("adaptive_aloha")

    def test_amber_run_writes_the_same_files(self):
        """What OAI checks: python -m amber.run writes byte for byte what the containers write together."""
        for name in ("registration", "adaptive_aloha", "gen2_q"):
            with self.subTest(name), tempfile.TemporaryDirectory() as containers, tempfile.TemporaryDirectory() as one:
                self.run_sessions(name, containers)
                with contextlib.redirect_stdout(io.StringIO()):
                    amber_run.run(os.path.join(SCENARIO_DIR, name + ".toml"), one)
                names = sorted(os.listdir(containers))
                self.assertEqual(names, sorted(os.listdir(one)))
                self.assertIn("reader_bs0_rx.csv", names)
                for file in names:
                    with open(os.path.join(containers, file)) as a, open(os.path.join(one, file)) as b:
                        self.assertEqual(a.read(), b.read(), file)

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
        from amber.proxies import Delivery, Uplink
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
