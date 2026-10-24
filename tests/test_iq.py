# tests/test_iq.py
"""Checks of amber.iq: the block plumbing (16-bit counts, control values, sums that saturate), the
Reader's and the Tags' control handshake (hello, up, bye), a Tag that is missing, and that a short
run of one Tag decodes a Tag data packet at the Reader."""

import os
import sys
import tempfile
import unittest

import numpy as np

from amber import iq, scenario
from amber.phy import params as rparams

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import SCENARIO_DIR, SHORT_MS, read_files  # noqa: E402

SINGLE = os.path.join(SCENARIO_DIR, "single_node.toml")


def load(sim_time_ms=SHORT_MS, **overrides):
    return scenario.load(SINGLE, {"run.sim_time_ms": sim_time_ms, **overrides})


class TestBlocks(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(iq.AIR_SAMPLES, rparams.SAMPLES_PER_MS)
        self.assertEqual(iq.AIR_SAMPLES, 5760)
        self.assertEqual(iq.BLOCK_SAMPLES, 5760 + 128)
        self.assertEqual(iq.BLOCK_BYTES, 23552)
        self.assertEqual(iq.CONTROL_INT16, 256)

    def test_block_bytes_are_checked(self):
        iq.check_block_bytes(iq.BLOCK_BYTES)
        with self.assertRaisesRegex(ValueError, "block_bytes"):
            iq.check_block_bytes(iq.BLOCK_BYTES + 4)

    def test_split_and_join_round_trip(self):
        air = np.arange(2 * iq.AIR_SAMPLES, dtype=np.int64) % 1000 - 500
        control = np.arange(iq.CONTROL_INT16, dtype=np.int64)
        block = iq.join_block(air, control)
        self.assertEqual(len(block), iq.BLOCK_BYTES)
        got_air, got_control = iq.split_block(block)
        np.testing.assert_array_equal(got_air, air)
        np.testing.assert_array_equal(got_control, control)

    def test_counts_round_trip(self):
        rng = np.random.default_rng(1)
        x = rng.normal(size=100) + 1j * rng.normal(size=100)
        scale = 1000.0
        counts = iq.to_counts(x, scale)
        self.assertEqual((counts.dtype, len(counts)), (np.dtype("int16"), 200))
        self.assertLessEqual(float(np.abs(iq.from_counts(counts, scale) - x).max()), 0.5 * 2 ** 0.5 / scale + 1e-12)
        # I and Q interleave
        self.assertEqual(list(iq.to_counts(np.array([1 + 2j]), 10.0)), [10, 20])

    def test_counts_clip_at_the_16_bit_limits(self):
        counts = iq.to_counts(np.array([1e9 - 1e9j]), 1.0)
        self.assertEqual(list(counts), [32767, -32768])

    def test_the_receiver_scale_puts_the_noise_at_one_count(self):
        noise_w = iq.thermal_noise_w(6.0)
        self.assertAlmostEqual(iq.up_scale(noise_w) * np.sqrt(noise_w / 2), iq.ADC_NOISE_COUNTS)
        # 6 dB more noise figure is 6 dB more noise power over the same bandwidth
        self.assertAlmostEqual(iq.thermal_noise_w(12.0) / noise_w, 10 ** 0.6)
        self.assertAlmostEqual(iq.w_to_dbm(iq.dbm_to_w(-37.0)), -37.0)

    def test_the_sum_of_strong_blocks_saturates_and_does_not_wrap(self):
        strong = np.full(2 * iq.AIR_SAMPLES, 30000, dtype="<i2")
        blocks = [np.concatenate([strong, np.zeros(iq.CONTROL_INT16, dtype="<i2")]).tobytes()] * 2
        air, _ = iq.split_block(iq.add_blocks(blocks))
        self.assertTrue(np.all(air == 32767))

    def test_the_sum_adds_the_control_of_each_tag(self):
        zeros = np.zeros(2 * iq.AIR_SAMPLES, dtype=np.int64)
        a, b = np.zeros(iq.CONTROL_INT16, dtype=np.int64), np.zeros(iq.CONTROL_INT16, dtype=np.int64)
        a[0], b[1] = iq.UP, iq.UP
        _, control = iq.split_block(iq.add_blocks([iq.join_block(zeros, a), iq.join_block(zeros, b)]))
        self.assertEqual(list(control[:2]), [iq.UP, iq.UP])

    def test_the_sdu_round_trips(self):
        self.assertEqual(iq.read_sdu(iq.make_sdu(300, 200)), (300, 200))
        self.assertEqual(iq.read_sdu(iq.make_sdu(1, 999)), (1, 255))   # the value is 8 bits
        self.assertIsNone(iq.read_sdu(b"\x00"))

    def test_the_paging_id_is_the_node_id_on_16_bits(self):
        bits = iq.paging_id(5)
        self.assertEqual((len(bits), bits[-3:]), (16, [1, 0, 1]))
        self.assertNotEqual(iq.paging_id(5), iq.paging_id(6))


class TestControl(unittest.TestCase):
    """The handshake: hello while idle, up in every millisecond, bye at the end."""

    def sessions(self, out, s=None):
        s = s or load(20)
        reader = iq.IqReaderSession(s, iq.BLOCK_BYTES, out)
        tag = iq.IqTagSession(s, s.NODE_IDS[0], iq.BLOCK_BYTES, out)
        return reader, tag

    def test_hello_up_bye(self):
        with tempfile.TemporaryDirectory() as out:
            reader, tag = self.sessions(out, load(5))
            marks = []
            block = reader.next_block()
            self.assertEqual(len(block), iq.BLOCK_BYTES)
            self.assertEqual(int(iq.split_block(block)[1][0]), iq.IDLE)
            answer, done = tag.reply(block)
            self.assertFalse(done)
            self.assertEqual(int(iq.split_block(answer)[1][tag.index]), iq.HELLO)
            self.assertFalse(reader.on_block(answer))
            done = False
            while not done:
                block = reader.next_block()
                control = iq.split_block(block)[1]
                answer, tag_done = tag.reply(block)
                mark = int(iq.split_block(answer)[1][tag.index])
                marks.append((int(control[0]), int(control[1]), mark))
                done = reader.on_block(answer)
                self.assertEqual(done, tag_done)
            self.assertEqual(marks, [(iq.MS, ms, iq.UP) for ms in range(5)] + [(iq.END, 0, iq.BYE)])

    def test_the_reader_waits_for_every_hello(self):
        with tempfile.TemporaryDirectory() as out:
            s = scenario.load(os.path.join(SCENARIO_DIR, "registration.toml"), {"run.sim_time_ms": 5})
            reader = iq.IqReaderSession(s, iq.BLOCK_BYTES, out)
            tags = [iq.IqTagSession(s, n, iq.BLOCK_BYTES, out) for n in s.NODE_IDS]
            idle = reader.next_block()
            answers = [t.reply(idle)[0] for t in tags]
            # Only some of the Tags are there: the Reader stays idle
            self.assertFalse(reader.on_block(iq.add_blocks(answers[:-1])))
            self.assertEqual(int(iq.split_block(reader.next_block())[1][0]), iq.IDLE)
            self.assertFalse(reader.on_block(iq.add_blocks(answers)))
            self.assertEqual(int(iq.split_block(reader.next_block())[1][0]), iq.MS)

    def test_a_missing_tag_is_an_error(self):
        with tempfile.TemporaryDirectory() as out:
            s = scenario.load(os.path.join(SCENARIO_DIR, "registration.toml"), {"run.sim_time_ms": 5})
            reader = iq.IqReaderSession(s, iq.BLOCK_BYTES, out)
            tags = [iq.IqTagSession(s, n, iq.BLOCK_BYTES, out) for n in s.NODE_IDS]
            idle = reader.next_block()
            self.assertFalse(reader.on_block(iq.add_blocks([t.reply(idle)[0] for t in tags])))
            block = reader.next_block()
            with self.assertRaisesRegex(RuntimeError, "did not answer"):
                reader.on_block(iq.add_blocks([t.reply(block)[0] for t in tags[1:]]))

    def test_a_tag_at_the_wrong_millisecond_is_an_error(self):
        with tempfile.TemporaryDirectory() as out:
            reader, tag = self.sessions(out)
            tag.reply(reader.next_block())
            reader.on_block(tag.reply(reader.next_block())[0])
            block = reader.next_block()
            tag.reply(block)
            with self.assertRaisesRegex(RuntimeError, "expected 1 ms"):
                tag.reply(block)

    def test_the_block_size_is_checked(self):
        with tempfile.TemporaryDirectory() as out:
            s = load(5)
            with self.assertRaisesRegex(ValueError, "block_bytes"):
                iq.IqReaderSession(s, 4096, out)
            with self.assertRaisesRegex(ValueError, "block_bytes"):
                iq.IqTagSession(s, 0, 4096, out)

    def test_the_block_of_a_millisecond_carries_its_number(self):
        with tempfile.TemporaryDirectory() as out:
            reader, tag = self.sessions(out)
            reader.on_block(tag.reply(reader.next_block())[0])
            ms = 0
            while ms < 3:
                block = reader.next_block()
                control = iq.split_block(block)[1]
                self.assertEqual((int(control[0]), int(control[1])), (iq.MS, ms))
                reader.on_block(tag.reply(block)[0])
                ms += 1

    def test_the_reader_carrier_is_always_on(self):
        with tempfile.TemporaryDirectory() as out:
            reader, tag = self.sessions(out, load(30))
            reader.on_block(tag.reply(reader.next_block())[0])
            powers = []
            for _ in range(30):
                block = reader.next_block()
                air = iq.from_counts(iq.split_block(block)[0], iq.TX_COUNTS)
                powers.append(float(np.mean(np.abs(air) ** 2)))
                reader.on_block(tag.reply(block)[0])
            self.assertGreater(min(powers), 0.3)
            self.assertLessEqual(max(powers), 1.0 + 1e-9)


class TestRun(unittest.TestCase):
    def test_one_tag_decodes_at_least_one_data_packet(self):
        with tempfile.TemporaryDirectory() as out:
            reader, tags = iq.run(load(), out)
            data = [r for r in reader.record.rx if r[2] == "data"]
            self.assertGreater(len(data), 0)
            self.assertTrue(all(r[3] == 0 for r in data), "every data packet is from node 0")
            # What the Reader decoded is what the Tag sent over the air
            sent = [(r.kind, r.payload) for r in tags[0].bsm.tx_records]
            self.assertGreaterEqual(len([k for k, _ in sent if k != "msg1"]), len(data))
            self.assertTrue(all(r[0] > 0 and r[8] < 0 for r in data), "time and RSSI in dBm")
            self.assertEqual(len(reader.record.tx), len(read_files(out)["reader_tx.csv"].splitlines()) - 1)

    def test_the_reader_ends_after_the_last_millisecond(self):
        with tempfile.TemporaryDirectory() as out:
            reader, _ = iq.run(load(50), out)
            self.assertTrue(reader._ending)
            self.assertEqual(reader._ms, 50)

    def test_same_run_same_files(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            iq.run(load(300), a)
            iq.run(load(300), b)
            self.assertEqual(read_files(a), read_files(b))

    def test_a_scenario_of_too_many_tags_is_refused(self):
        s = load(5)
        s.NODE_IDS = list(range(iq.MAX_TAGS + 1))
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaisesRegex(ValueError, "at most"):
                iq.IqReaderSession(s, iq.BLOCK_BYTES, out)


class TestAirOnly(unittest.TestCase):
    """Air-only blocks, for rfsim_hub: no control, and the same files as blocks with control."""

    def test_sizes(self):
        self.assertEqual(iq.AIR_BLOCK_BYTES, 5760 * 4)
        self.assertFalse(iq.check_block_bytes(iq.AIR_BLOCK_BYTES))
        self.assertTrue(iq.check_block_bytes(iq.BLOCK_BYTES))

    def test_block_n_is_millisecond_n_and_both_sides_stop_together(self):
        with tempfile.TemporaryDirectory() as out:
            s = load(7)
            reader = iq.IqReaderSession(s, iq.AIR_BLOCK_BYTES, out)
            tag = iq.IqTagSession(s, s.NODE_IDS[0], iq.AIR_BLOCK_BYTES, out)
            ends = []
            for ms in range(7):
                block = reader.next_block()
                self.assertEqual(len(block), iq.AIR_BLOCK_BYTES)
                answer, tag_done = tag.reply(block)
                self.assertEqual(len(answer), iq.AIR_BLOCK_BYTES)
                ends.append((reader.on_block(answer), tag_done))
            self.assertEqual(ends, [(False, False)] * 6 + [(True, True)])

    def test_same_files_as_with_control(self):
        for name in ("single_node.toml", "registration.toml"):
            with self.subTest(name), tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
                s = lambda: scenario.load(os.path.join(SCENARIO_DIR, name), {"run.sim_time_ms": 600})
                iq.run(s(), a)
                iq.run(s(), b, iq.AIR_BLOCK_BYTES)
                self.assertEqual(read_files(a), read_files(b))


# topology 1 in a 5G room: the gNB is the Reader
GNB_READER = {"network.cell": "n28-25prb", "network.r2d": "gnb", "network.d2r": "gnb"}


class TestCarrierNode(unittest.TestCase):
    """The carrier node of a room whose carrier comes from a node of its own."""

    def test_sends_the_carrier_until_the_run_ends(self):
        with tempfile.TemporaryDirectory() as out:
            node = iq.IqCarrierSession(load(5, **GNB_READER, **{"network.cw": "cw-node-ul"}), iq.AIR_BLOCK_BYTES, out)
            carrier = np.tile(np.array([iq.TX_COUNTS, 0], dtype="<i2"), iq.AIR_SAMPLES).tobytes()
            ends = []
            for _ in range(5):
                self.assertEqual(node.next_block(), carrier)
                ends.append(node.on_block(bytes(iq.AIR_BLOCK_BYTES)))
            self.assertEqual(ends, [False] * 4 + [True])
            node.finish()
            self.assertEqual(os.listdir(out), [])

    def test_needs_a_carrier_node_and_air_only_blocks(self):
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaisesRegex(ValueError, "air-only"):
                iq.IqCarrierSession(load(5, **GNB_READER, **{"network.cw": "cw-node-ul"}), iq.BLOCK_BYTES, out)
            with self.assertRaisesRegex(ValueError, "not a carrier node"):
                iq.IqCarrierSession(load(5, **GNB_READER, **{"network.cw": "gnb-dl"}), iq.AIR_BLOCK_BYTES, out)


if __name__ == "__main__":
    unittest.main()
