# tests/test_output.py
"""Checks amber.output and python -m amber.run: every demo writes the Reader's and every Tag's CSV
files, with one header taken from the record it logs, next to its scenario."""

import contextlib
import csv
import dataclasses
import glob
import io
import os
import tempfile
import types
import unittest

from amber import output
from amber import run as amber_run
from amber.backscatter import RxRecord, TxRecord
from amber.bsengine import RxPacket, TxPacket
from amber.hosts import load_scenario

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIO_DIR = os.path.join(HERE, "scenarios")
DEMO_DIR = os.path.join(HERE, "..", "demo_experiments")


def rows(path):
    with open(path, newline="") as f:
        return list(csv.reader(f))


def header(record_type):
    return [field.name for field in dataclasses.fields(record_type)]


def quiet_run(path, out=None):
    with contextlib.redirect_stdout(io.StringIO()) as printed:
        out = amber_run.run(path, out)
    return out, printed.getvalue()


class RunTest(unittest.TestCase):
    def test_every_demo(self):
        for path in sorted(glob.glob(os.path.join(DEMO_DIR, "*", "*.toml"))):
            with self.subTest(os.path.relpath(path, DEMO_DIR)), tempfile.TemporaryDirectory() as out:
                _, printed = quiet_run(path, out)
                self.assertIn("sent=", printed)
                s = load_scenario(path)
                for bs_id in s.BS_IDS:
                    self.assertEqual(rows(os.path.join(out, f"reader_bs{bs_id}_rx.csv"))[0], header(RxPacket))
                    self.assertEqual(rows(os.path.join(out, f"reader_bs{bs_id}_tx.csv"))[0], header(TxPacket))
                self.assertGreater(len(rows(os.path.join(out, f"reader_bs{s.BS_IDS[0]}_rx.csv"))), 1)
                for node_id in s.NODE_IDS:
                    self.assertEqual(rows(os.path.join(out, f"tag{node_id}_tx.csv"))[0], header(TxRecord))
                    self.assertEqual(rows(os.path.join(out, f"tag{node_id}_rx.csv"))[0], header(RxRecord))

    def test_files_go_next_to_the_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "single_node.toml")
            with open(os.path.join(SCENARIO_DIR, "single_node.toml")) as f:
                text = f.read().replace('"../../demo_experiments/', repr(os.path.abspath(DEMO_DIR) + "/").replace("'", '"')[:-1])
            with open(path, "w") as f:
                f.write(text)
            out, _ = quiet_run(path)
            self.assertEqual(out, os.path.join(tmp, "output", "single_node"))
            self.assertTrue(os.path.exists(os.path.join(out, "reader_bs0_rx.csv")))

    def test_protocol_files(self):
        with tempfile.TemporaryDirectory() as out:
            quiet_run(os.path.join(SCENARIO_DIR, "adaptive_aloha.toml"), out)
            adaptation = rows(os.path.join(out, "adaptation.csv"))
            self.assertEqual(adaptation[0], ["time_s", "n_slots", "heard", "collided"])
            self.assertGreater(len(adaptation), 1)
        with tempfile.TemporaryDirectory() as out:
            quiet_run(os.path.join(SCENARIO_DIR, "registration.toml"), out)
            self.assertFalse(os.path.exists(os.path.join(out, "adaptation.csv")))


class NoPacketsTest(unittest.TestCase):
    def test_files_with_no_rows_hold_their_header(self):
        bs = types.SimpleNamespace(id=0, rx_packets=[], tx_packets=[])
        tag = types.SimpleNamespace(tx_records=[], rx_records=[])
        with tempfile.TemporaryDirectory() as out:
            output.write_reader_files(out, None, [bs])
            output.write_tag_files(out, 3, tag)
            self.assertEqual(rows(os.path.join(out, "reader_bs0_rx.csv")), [header(RxPacket)])
            self.assertEqual(rows(os.path.join(out, "reader_bs0_tx.csv")), [header(TxPacket)])
            self.assertEqual(rows(os.path.join(out, "tag3_tx.csv")), [header(TxRecord)])
            self.assertEqual(rows(os.path.join(out, "tag3_rx.csv")), [header(RxRecord)])


if __name__ == "__main__":
    unittest.main()
