# tests/split/test_seeds.py
"""Checks the seeds of Amber's four demos as demo_experiments/split has them: SEED decides a run,
and changing it changes what the nodes draw but not where they stand (custom_protocol takes its
layout from LAYOUT_SEED)."""

import os
import unittest

from amber.split.hosts import build_one_process, load_scenario

DEMO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "demo_experiments", "split")
DEMOS = ("broadcast", "broadcast_sic", "unicast", "custom_protocol")
RUN_MS = 5000  # long enough for every demo to transmit


def run(name, seed):
    scenario = load_scenario(os.path.join(DEMO_DIR, name + ".py"))
    scenario.SEED = seed
    env, bss, bsms = build_one_process(scenario)
    env.run(until=RUN_MS)
    layout = [(bsm.node.id, bsm.node.x, bsm.node.y) for bsm in bsms]
    return layout, [bs.rx_packets for bs in bss], [bsm.tx_records for bsm in bsms]


class SeedTest(unittest.TestCase):
    def test_same_seed_same_run(self):
        for name in DEMOS:
            with self.subTest(name):
                self.assertEqual(run(name, 1), run(name, 1))

    def test_other_seed_same_layout_other_draws(self):
        for name in DEMOS:
            with self.subTest(name):
                layout, rx, tx = run(name, 1)
                other_layout, other_rx, other_tx = run(name, 2)
                self.assertEqual(layout, other_layout)
                self.assertGreater(sum(map(len, tx)), 0)
                self.assertNotEqual(tx, other_tx)
                self.assertNotEqual(rx, other_rx)

    def test_custom_protocol_layout_is_the_demos(self):
        scenario = load_scenario(os.path.join(DEMO_DIR, "custom_protocol.py"))
        self.assertEqual((scenario.SEED, scenario.LAYOUT_SEED), (7, 7))


if __name__ == "__main__":
    unittest.main()
