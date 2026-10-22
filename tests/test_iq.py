# tests/test_iq.py
"""Checks of amber.phy and amber.iq: the waveforms decode when they should and fail when they
should, the carrier matters, commands survive their bit fields, and a run is the same whether its
blocks go through the sessions one by one or are added up by iq.run()."""

import math
import os
import tempfile
import unittest

import numpy as np

from amber import iq, phy, scenario
from amber.session import ReaderSession, TagSession

SCENARIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")
IQ_SCENARIO = os.path.join(SCENARIO_DIR, "gen2_q.toml")
CFG = phy.PhyConfig()
NOISE_W = phy.thermal_noise_w(6.0)


def downlink(bits, power_dbm, sensitivity_dbm=-45.0, seed=0, carrier_on=True):
    """The commands a Tag decodes from bits sent at power_dbm."""
    tx = phy.ReaderTransmitter(CFG)
    tx.queue(bits)
    det = phy.EnvelopeDetector(CFG, sensitivity_dbm, np.random.default_rng(seed))
    got = []
    for _ in range(4):
        got += det.receive(tx.next_ms(carrier_on) * math.sqrt(phy.dbm_to_w(power_dbm)))
    return got


def uplink(powers_dbm, sic=True, seed=0, shifts=None, carrier=None):
    """(node ids decoded, collisions) for Tags 10, 11, ... reflecting at once."""
    rng = np.random.default_rng(seed)
    shifts = shifts or [0] * len(powers_dbm)
    rx = phy.ReaderReceiver(CFG, shifts, NOISE_W, sic, rng, range(10, 10 + len(powers_dbm)))
    tags = [phy.TagReflector(CFG, s, rng.uniform(0, 2 * math.pi)) for s in shifts]
    for i, (t, p) in enumerate(zip(tags, powers_dbm)):
        t.queue(10 + i, 1000 + i, p)
    scale = phy.up_scale(NOISE_W)
    decoded, collisions = [], 0
    for ms in range(3):
        c = np.ones(phy.AIR_SAMPLES, dtype=complex) if carrier is None else carrier
        # Each Tag writes its own 16-bit samples and the Reader gets their saturated sum, as in iq.py
        x = sum(phy.to_counts(t.next_ms(c, ms * phy.AIR_SAMPLES), scale).astype(np.int64) for t in tags)
        d, n = rx.receive(ms, phy.from_counts(np.clip(x, -32768, 32767), scale))
        decoded += d
        collisions += n
    return sorted(d.node_id for d in decoded), collisions


class TestPhy(unittest.TestCase):
    def test_crc_catches_a_flipped_bit(self):
        bits = [1, 0, 1, 1, 0, 0, 1, 0] * 4
        flipped = list(bits)
        flipped[5] ^= 1
        self.assertNotEqual(phy.crc16(bits), phy.crc16(flipped))

    def test_a_command_decodes_above_sensitivity_and_not_below(self):
        bits = [1, 0, 0, 1, 1, 1, 0, 1] * 5
        self.assertEqual(downlink(bits, -30.0), [bits])
        self.assertEqual(downlink(bits, -60.0), [])

    def test_no_carrier_no_command(self):
        tx = phy.ReaderTransmitter(CFG)
        self.assertEqual(float(np.abs(tx.next_ms(False)).max()), 0.0)
        self.assertEqual(float(np.abs(tx.next_ms(True)).min()), 1.0)

    def test_one_tag_decodes(self):
        self.assertEqual(uplink([-80.0]), ([10], 0))

    def test_a_weak_tag_is_heard_beside_a_tag_next_to_the_reader(self):
        # The receiver gain follows the noise, not the strongest Tag: -95 dBm still decodes beside
        # a Tag at -20 dBm on another subcarrier
        self.assertEqual(uplink([-20.0, -95.0], shifts=[0, 1]), ([10, 11], 0))

    def test_the_sum_of_strong_tags_saturates_and_does_not_wrap(self):
        strong = np.full(2 * phy.AIR_SAMPLES, 30000, dtype="<i2")
        blocks = [np.concatenate([strong, np.zeros(iq.CONTROL_INT16, dtype="<i2")]).tobytes()] * 2
        air, _ = iq.split_block(iq.add_blocks(blocks))
        self.assertTrue(np.all(air == 32767))

    def test_a_tag_far_below_the_noise_is_not_heard(self):
        self.assertEqual(uplink([-125.0]), ([], 0))

    def test_without_carrier_there_is_nothing_to_reflect(self):
        self.assertEqual(uplink([-80.0], carrier=np.zeros(phy.AIR_SAMPLES, dtype=complex)), ([], 0))

    def test_sic_decodes_the_weaker_tag_after_the_stronger(self):
        self.assertEqual(uplink([-70.0, -80.0], sic=True), ([10, 11], 0))

    def test_without_sic_the_weaker_tag_is_a_collision(self):
        self.assertEqual(uplink([-70.0, -80.0], sic=False), ([10], 1))

    def test_sic_separates_three_tags_with_nearly_the_same_data(self):
        # Small IDs and values are mostly the same zeros; scrambled per Tag, the frames still differ
        for seed in range(8):
            self.assertEqual(uplink([-57.0, -64.0, -67.0], seed=seed), ([10, 11, 12], 0))

    def test_many_equal_tags_collide(self):
        decoded, collisions = uplink([-80.0] * 6)
        self.assertEqual(collisions, 1)
        self.assertLess(len(decoded), 6)

    def test_different_subcarriers_do_not_collide(self):
        self.assertEqual(uplink([-80.0, -80.0], shifts=[0, 1]), ([10, 11], 0))

    def test_rates_must_fit_the_millisecond(self):
        with self.assertRaises(ValueError):
            phy.PhyConfig(r2d_kbps=7)


class TestCommands(unittest.TestCase):
    def setUp(self):
        self.s = scenario.load(IQ_SCENARIO)
        self.codec = iq.CommandCodec(self.s.protocol, self.s.NODE_IDS, 0)

    def round_trip(self, node_ids, cmd, data, node_id, end_ms=100):
        return self.codec.decode(self.codec.encode(node_ids, cmd, data, end_ms), node_id, end_ms)

    def test_a_broadcast_with_slots(self):
        data = {"cmd": "query", "target": -1, "q": 5, "rx_slots": [(101.0, 111.0), (111.0, 121.0)]}
        d = self.round_trip(self.s.NODE_IDS, "query", data, 3)
        self.assertEqual((d.cmd, d.data), ("query", data))

    def test_a_command_for_one_tag_reaches_only_it(self):
        data = {"cmd": "send_data", "target": 4, "rx_slots": [(105.0, 115.0)]}
        self.assertEqual(self.round_trip([4], "send_data", data, 4).data, data)
        self.assertIsNone(self.round_trip([4], "send_data", data, 5))

    def test_an_ack(self):
        d = self.round_trip([2], "ack", {}, 2)
        self.assertEqual((d.cmd, d.data), ("ack", {}))

    def test_a_field_without_a_width_is_refused(self):
        with self.assertRaises(ValueError):
            self.codec.encode(self.s.NODE_IDS, "query", {"cmd": "query", "target": -1, "colour": 1}, 0)

    def test_scattered_slots_are_refused(self):
        with self.assertRaises(ValueError):
            self.codec.encode(self.s.NODE_IDS, "query", {"cmd": "query", "rx_slots": [(1, 2), (5, 6)]}, 0)


class TestRun(unittest.TestCase):
    def run_iq(self, out):
        iq.run(scenario.load(IQ_SCENARIO), out)
        return {f: open(os.path.join(out, f)).read() for f in sorted(os.listdir(out))}

    def test_the_sessions_give_what_iq_run_gives(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            expected = self.run_iq(a)
            path = IQ_SCENARIO
            reader = ReaderSession(path, iq.BLOCK_BYTES, b)
            self.assertTrue(reader.reads_sum)
            tags = [TagSession(path, n, iq.BLOCK_BYTES, b) for n in reader.scenario.NODE_IDS]
            done = False
            while not done:
                block = reader.next_block()
                # Each Tag's block on its own, added up by the Reader
                done = reader.on_blocks([t.reply(block)[0] for t in tags])
            reader.finish()
            for t in tags:
                t.finish()
            got = {f: open(os.path.join(b, f)).read() for f in sorted(os.listdir(b))}
            self.assertEqual(got, expected)
            self.assertGreater(expected["reader_bs0_rx.csv"].count("\n"), 2)

    def test_the_carrier_is_always_on_and_the_tags_live_on_it(self):
        with tempfile.TemporaryDirectory() as out:
            reader = ReaderSession(IQ_SCENARIO, iq.BLOCK_BYTES, out)
            tags = [TagSession(IQ_SCENARIO, n, iq.BLOCK_BYTES, out) for n in reader.scenario.NODE_IDS]
            powers, fractions, done = [], set(), False
            while not done:
                block = reader.next_block()
                air, control = iq.split_block(block)
                if int(control[0]) == iq.MS:
                    powers.append(float(np.mean(np.abs(phy.from_counts(air, phy.TX_COUNTS)) ** 2)))
                replies = [t.reply(block)[0] for t in tags]
                if int(control[0]) == iq.MS:
                    # Each Tag harvests from the carrier it received in this block
                    for t in tags:
                        self.assertAlmostEqual(t.node.carrier_fraction, powers[-1], places=6)
                    fractions.add(round(tags[0].node.carrier_fraction, 6))
                done = reader.on_blocks(replies)
            # On in every millisecond, idle or listening; only a command keys part of it off
            self.assertEqual(len(powers), reader.scenario.SIM_TIME_MS)
            self.assertGreater(min(powers), 0.3)
            self.assertIn(1.0, fractions)
            self.assertLess(min(fractions), 1.0)

    def test_iq_mode_needs_one_base_station(self):
        with open(IQ_SCENARIO) as f:
            text = f.read()
        text += "\n[[base_station]]\nid = 1\nx = 50\ny = 0\n"
        with tempfile.NamedTemporaryFile("w", suffix=".toml", dir=SCENARIO_DIR, delete=False) as f:
            f.write(text)
        try:
            with self.assertRaisesRegex(ValueError, "exactly one base station"):
                scenario.load(f.name)
        finally:
            os.remove(f.name)


if __name__ == "__main__":
    unittest.main()
