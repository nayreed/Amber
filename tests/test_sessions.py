# tests/test_sessions.py
"""Checks the Reader and Tag sessions that amber-reader and amber-tag drive, one block of IQ samples
per millisecond, against iq.run(), which python -m amber.run uses for the same scenario.

The Tags answer in reverse order and Python's global random generator is left in an unrelated state
before each one does, as separate containers would leave it, and the Reader reads the sum of their
blocks. The records must be identical, so a node that drew from the global generator would fail.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest

from amber import iq, scenario
from amber import run as amber_run
from amber.session import ReaderSession, TagSession

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import SCENARIO_DIR, records, run_one_machine, run_sessions, shortened  # noqa: E402


def load(name, sim_time_ms):
    s = scenario.load(os.path.join(SCENARIO_DIR, name + ".toml"))
    s.SIM_TIME_MS = sim_time_ms
    return s


class SessionTest(unittest.TestCase):
    def check(self, name, sim_time_ms):
        one = records(*run_one_machine(load(name, sim_time_ms)))
        self.assertGreater(len(one[0][0][0]), 0, "scenario too short to exercise uplinks")
        with tempfile.TemporaryDirectory() as out:
            reader, tags = run_sessions(load(name, sim_time_ms), out)
            self.assertEqual(one, records(reader, tags))
            with open(os.path.join(out, "reader_bs0_rx.csv")) as f:
                self.assertEqual(len(f.readlines()), len(one[0][0][0]) + 1)
            for tag in tags:
                for side in ("tx", "rx"):
                    self.assertTrue(os.path.exists(os.path.join(out, f"tag{tag.node_id}_{side}.csv")))

    def test_single_node(self):
        self.check("single_node", 2001)

    def test_registration_four_tags(self):
        self.check("registration", 3001)

    def test_adaptive_aloha_twelve_tags(self):
        self.check("adaptive_aloha", 2001)

    def test_amber_run_writes_the_same_files(self):
        """What OAI checks: python -m amber.run writes byte for byte what the containers write together."""
        for name in ("registration", "adaptive_aloha", "gen2_q_iq"):
            with self.subTest(name), shortened(os.path.join(SCENARIO_DIR, name + ".toml"), 1501) as path, \
                    tempfile.TemporaryDirectory() as containers, tempfile.TemporaryDirectory() as one:
                reader = ReaderSession(path, iq.BLOCK_BYTES, containers)
                self.assertTrue(reader.reads_sum)
                tags = [TagSession(path, n, iq.BLOCK_BYTES, containers) for n in reader.scenario.NODE_IDS]
                done = False
                while not done:
                    block = reader.next_block()
                    done = reader.on_blocks([t.reply(block)[0] for t in tags])
                reader.finish()
                for t in tags:
                    t.finish()
                with contextlib.redirect_stdout(io.StringIO()):
                    amber_run.run(path, one)
                names = sorted(os.listdir(containers))
                self.assertEqual(names, sorted(os.listdir(one)))
                self.assertIn("reader_bs0_rx.csv", names)
                for file in names:
                    with open(os.path.join(containers, file)) as a, open(os.path.join(one, file)) as b:
                        self.assertEqual(a.read(), b.read(), file)

    def started_reader(self):
        """A Reader of the 4-node registration scenario that has heard every hello."""
        path = os.path.join(SCENARIO_DIR, "registration.toml")
        reader = ReaderSession(path, iq.BLOCK_BYTES, ".")
        tags = [TagSession(path, node_id, iq.BLOCK_BYTES, ".") for node_id in reader.scenario.NODE_IDS]
        idle = reader.next_block()
        self.assertFalse(reader.on_blocks([iq.add_blocks([tag.reply(idle)[0] for tag in tags])]))
        return reader, tags

    def test_a_missing_tag_is_an_error(self):
        reader, tags = self.started_reader()
        block = reader.next_block()
        with self.assertRaisesRegex(RuntimeError, "did not answer"):
            reader.on_blocks([iq.add_blocks([tag.reply(block)[0] for tag in tags[1:]])])

    def test_a_tag_at_the_wrong_millisecond_is_an_error(self):
        reader, tags = self.started_reader()
        block = reader.next_block()
        tags[0].reply(block)
        with self.assertRaisesRegex(RuntimeError, "expected 1 ms"):
            tags[0].reply(block)

    def test_the_containers_need_a_seed(self):
        with shortened(os.path.join(SCENARIO_DIR, "registration.toml"), 100) as path:
            with open(path) as f:
                text = f.read().replace("seed = 12345\n", "")
            with open(path, "w") as f:
                f.write(text)
            with self.assertRaisesRegex(ValueError, "need a seed"):
                ReaderSession(path, iq.BLOCK_BYTES, ".")
            with self.assertRaisesRegex(ValueError, "need a seed"):
                TagSession(path, 0, iq.BLOCK_BYTES, ".")


if __name__ == "__main__":
    unittest.main()
