# tests/split/test_output.py
"""Checks amber.output, amber.run and amber.split.export: python -m amber.run writes the CSV files
of every demo next to it, and a split run's export equals the files of the run in one process."""

import contextlib
import glob
import io
import os
import tempfile
import unittest

import pandas as pd

from amber import output, scenario
from amber import run as amber_run
from amber.split import export, reference
from amber.split.hosts import build_one_process

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIO_DIR = os.path.join(HERE, "scenarios")
DEMO_DIR = os.path.join(HERE, "..", "..", "demo_experiments")


def run(path, seed=12345):
    s = scenario.load(path, {"run.seed": seed})
    env, bss, bsms = build_one_process(s)
    env.run(until=s.SIM_TIME_MS)
    return s, bss, bsms


def read(path):
    with open(path) as f:
        return f.read()


class RunTest(unittest.TestCase):
    """python -m amber.run on every demo scenario."""

    def test_every_demo(self):
        for path in sorted(glob.glob(os.path.join(DEMO_DIR, "*", "*.toml"))):
            with self.subTest(path), tempfile.TemporaryDirectory() as out:
                with contextlib.redirect_stdout(io.StringIO()) as printed:
                    amber_run.run(path, out)
                self.assertIn("sent=", printed.getvalue())
                for file, columns in (("rx_packets.csv", output.RX_COLUMNS), ("tx_records.csv", output.TX_COLUMNS)):
                    frame = pd.read_csv(os.path.join(out, file))
                    self.assertEqual(tuple(frame.columns), columns)
                    self.assertGreater(len(frame), 0, file)

    def test_files_equal_the_run_and_go_next_to_the_scenario(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as one:
            path = os.path.join(tmp, "adaptive_aloha.toml")
            source = os.path.join(DEMO_DIR, "adaptive_aloha", "adaptive_aloha.toml")
            with open(source) as f:  # seeded with 7; the trace path is made absolute for the copy
                text = f.read().replace('"../threehundred_seconds_stable.xlsx"',
                                        repr(os.path.abspath(os.path.join(DEMO_DIR, "threehundred_seconds_stable.xlsx"))).replace("'", '"'))
            with open(path, "w") as f:
                f.write(text)
            with contextlib.redirect_stdout(io.StringIO()):
                out = amber_run.run(path)
            self.assertEqual(out, os.path.join(tmp, "output", "adaptive_aloha"))
            s, bss, bsms = run(path, seed=7)
            output.write_packets(one, bss, bsms)
            output.write_protocol_outputs(one, s.protocol, bss)
            for file in ("rx_packets.csv", "tx_records.csv", "adaptation.csv"):
                self.assertEqual(read(os.path.join(one, file)), read(os.path.join(out, file)), file)

    def test_no_packets(self):
        with tempfile.TemporaryDirectory() as out:
            output.write_packet_rows(out, [], [])
            self.assertEqual(read(os.path.join(out, "rx_packets.csv")).strip(), ",".join(output.RX_COLUMNS))
            self.assertEqual(read(os.path.join(out, "tx_records.csv")).strip(), ",".join(output.TX_COLUMNS))


class ExportTest(unittest.TestCase):
    """amber.split.export on a split run's logs (which equal those of amber.split.reference, see
    test_split_equivalence) against write_packets() on the run in one process."""

    def test_export_equals_one_process(self):
        for name in ("broadcast", "registration", "adaptive_aloha"):
            path = os.path.join(SCENARIO_DIR, name + ".toml")
            with self.subTest(name), tempfile.TemporaryDirectory() as logs, tempfile.TemporaryDirectory() as one:
                reference.run(path, logs)
                export.run(path, logs)
                _, bss, bsms = run(path)
                output.write_packets(one, bss, bsms)
                for file in ("rx_packets.csv", "tx_records.csv"):
                    self.assertEqual(read(os.path.join(one, file)), read(os.path.join(logs, file)), file)

    def test_adaptation_csv_from_the_reader(self):
        with tempfile.TemporaryDirectory() as logs:
            reference.run(os.path.join(SCENARIO_DIR, "adaptive_aloha.toml"), logs)
            frame = pd.read_csv(os.path.join(logs, "adaptation.csv"))
            self.assertEqual(list(frame.columns), ["time_s", "n_slots", "heard", "collided"])
            self.assertGreater(len(frame), 0)
        with tempfile.TemporaryDirectory() as logs:
            reference.run(os.path.join(SCENARIO_DIR, "registration.toml"), logs)
            self.assertFalse(os.path.exists(os.path.join(logs, "adaptation.csv")))

    def test_missing_logs(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(FileNotFoundError, "no log of the Reader"):
                export.run(os.path.join(SCENARIO_DIR, "registration.toml"), empty)


if __name__ == "__main__":
    unittest.main()
