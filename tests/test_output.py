# tests/test_output.py
"""Checks amber.output and python -m amber.run: every demo writes the Reader's and every Tag's CSV
files, with one header taken from the record it logs, next to its scenario."""

import contextlib
import csv
import dataclasses
import glob
import io
import os
import sys
import tempfile
import types
import unittest

from amber import iq, output, summary
from amber import run as amber_run
from amber.backscatter import RxRecord, TxRecord
from amber.hosts import load_scenario

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import LONG_MS, SHORT_MS, shortened  # noqa: E402

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
            s = load_scenario(path)
            # The Tags charge first, and more of them take longer to simulate
            ms = LONG_MS if len(s.NODE_IDS) <= 4 else SHORT_MS
            with self.subTest(os.path.relpath(path, DEMO_DIR)), shortened(path, ms) as short, \
                    tempfile.TemporaryDirectory() as out:
                _, printed = quiet_run(short, out)
                self.assertIn("sent=", printed)
                self.assertEqual(rows(os.path.join(out, "reader_rx.csv"))[0], list(iq.RX_COLUMNS))
                self.assertEqual(rows(os.path.join(out, "reader_tx.csv"))[0], list(iq.TX_COLUMNS))
                self.assertGreater(len(rows(os.path.join(out, "reader_tx.csv"))), 1)
                self.assertGreater(len(rows(os.path.join(out, "reader_rx.csv"))), 1)
                for node_id in s.NODE_IDS:
                    self.assertEqual(rows(os.path.join(out, f"tag{node_id}_tx.csv"))[0], header(TxRecord))
                    self.assertEqual(rows(os.path.join(out, f"tag{node_id}_rx.csv"))[0], header(RxRecord))

    def test_files_go_next_to_the_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "single_node.toml")
            with open(os.path.join(SCENARIO_DIR, "single_node.toml")) as f:
                text = f.read().replace('"../../demo_experiments/', repr(os.path.abspath(DEMO_DIR) + "/").replace("'", '"')[:-1])
            text = text.replace("sim_time_ms = 5001", "sim_time_ms = 50")
            with open(path, "w") as f:
                f.write(text)
            out, _ = quiet_run(path)
            self.assertEqual(out, os.path.join(tmp, "output", "single_node"))
            self.assertTrue(os.path.exists(os.path.join(out, "reader_rx.csv")))

    def test_the_printed_results_are_the_summary_of_the_files(self):
        with shortened(os.path.join(SCENARIO_DIR, "single_node.toml"), SHORT_MS) as path, \
                tempfile.TemporaryDirectory() as out:
            _, printed = quiet_run(path, out)
            s = summary.summarize(out, SHORT_MS)
            self.assertIn(f"sent={s['sent']} decoded={s['decoded']} collided={s['collided']} msg1={s['msg1']}", printed)
            self.assertGreater(s["decoded"], 0)
            self.assertEqual(s["per_node"]["0"], (s["sent"], s["decoded"], s["msg1"]))
            self.assertAlmostEqual(s["throughput"], s["decoded"] / (SHORT_MS / 1000))

    def test_protocol_files(self):
        with tempfile.TemporaryDirectory() as out:
            with shortened(os.path.join(SCENARIO_DIR, "adaptive_aloha.toml"), SHORT_MS) as path:
                quiet_run(path, out)
            adaptation = rows(os.path.join(out, "adaptation.csv"))
            self.assertEqual(adaptation[0], ["time_s", "n_occasions", "heard", "collided"])
            self.assertGreater(len(adaptation), 1)
            self.assertFalse(os.path.exists(os.path.join(out, "q.csv")))
        with tempfile.TemporaryDirectory() as out:
            with shortened(os.path.join(SCENARIO_DIR, "single_node.toml"), 100) as path:
                quiet_run(path, out)
            self.assertFalse(os.path.exists(os.path.join(out, "adaptation.csv")))
            self.assertFalse(os.path.exists(os.path.join(out, "q.csv")))


class NoPacketsTest(unittest.TestCase):
    def test_files_with_no_rows_hold_their_header(self):
        tag = types.SimpleNamespace(tx_records=[], rx_records=[])
        with tempfile.TemporaryDirectory() as out:
            output.write_reader_files(out, None, iq.ReaderRecord(), iq.TX_COLUMNS, iq.RX_COLUMNS)
            output.write_tag_files(out, 3, tag)
            self.assertEqual(rows(os.path.join(out, "reader_rx.csv")), [list(iq.RX_COLUMNS)])
            self.assertEqual(rows(os.path.join(out, "reader_tx.csv")), [list(iq.TX_COLUMNS)])
            self.assertEqual(rows(os.path.join(out, "tag3_tx.csv")), [header(TxRecord)])
            self.assertEqual(rows(os.path.join(out, "tag3_rx.csv")), [header(RxRecord)])

    def test_a_tag_record_is_written_field_by_field(self):
        tag = types.SimpleNamespace(tx_records=[TxRecord(5.0, "data", 150, 2, 1)], rx_records=[RxRecord(9.0, "00ff")])
        with tempfile.TemporaryDirectory() as out:
            output.write_tag_files(out, 1, tag)
            self.assertEqual(rows(os.path.join(out, "tag1_tx.csv"))[1], ["5.0", "data", "150", "2", "1"])
            self.assertEqual(rows(os.path.join(out, "tag1_rx.csv"))[1], ["9.0", "00ff"])

    def test_the_protocol_adds_its_files(self):
        protocol = types.SimpleNamespace(outputs=lambda: {"extra.csv": (("a", "b"), [(1, 2)])})
        with tempfile.TemporaryDirectory() as out:
            output.write_reader_files(out, protocol, iq.ReaderRecord(), iq.TX_COLUMNS, iq.RX_COLUMNS)
            self.assertEqual(rows(os.path.join(out, "extra.csv")), [["a", "b"], ["1", "2"]])


if __name__ == "__main__":
    unittest.main()
