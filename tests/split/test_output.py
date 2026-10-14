# tests/split/test_output.py
"""Checks amber.output and amber.split.export: the CSV files equal those the demos' own code
writes for the same run, and a split run's export equals the files of the run in one process."""

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest

import pandas as pd

from amber import output, scenario
from amber.split import export, reference
from amber.split.hosts import build_one_process

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIO_DIR = os.path.join(HERE, "scenarios")
DEMO_DIR = os.path.join(HERE, "..", "..", "demo_experiments")


def demo_module(name):
    """A demo script, imported (its __main__ part does not run)."""
    path = os.path.join(DEMO_DIR, name, name + ("_demo.py"))
    spec = importlib.util.spec_from_file_location(f"{name}_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(path, seed=12345):
    s = scenario.load(path, {"run.seed": seed})
    env, bss, bsms = build_one_process(s)
    env.run(until=s.SIM_TIME_MS)
    return s, bss, bsms


def read(path):
    with open(path) as f:
        return f.read()


class DemoFilesTest(unittest.TestCase):
    """write_packets() against each demo's own save_packets(), on the same run."""

    def demo_files(self, name, bss, bsms, out):
        module = demo_module(name)
        module.OUTPUT_DIR = out
        with contextlib.redirect_stdout(io.StringIO()):
            module.save_packets(bss[0], bsms)

    def test_three_tag_demos(self):
        for name in ("broadcast", "broadcast_sic", "unicast"):
            with self.subTest(name), tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
                _, bss, bsms = run(os.path.join(DEMO_DIR, name, name + ".toml"))
                self.demo_files(name, bss, bsms, old)
                output.write_packets(new, bss, bsms)
                for file in ("rx_packets.csv", "tx_records.csv"):
                    self.assertEqual(read(os.path.join(old, file)), read(os.path.join(new, file)), file)

    def test_custom_protocol_demo(self):
        # This demo writes fewer columns of the same rows, and adaptation.csv.
        with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
            s, bss, bsms = run(os.path.join(DEMO_DIR, "custom_protocol", "custom_protocol.toml"), seed=7)
            self.demo_files("custom_protocol", bss, bsms, old)
            output.write_packets(new, bss, bsms)
            output.write_protocol_outputs(new, s.protocol, bss)
            for file in ("rx_packets.csv", "tx_records.csv"):
                ours, theirs = pd.read_csv(os.path.join(new, file)), pd.read_csv(os.path.join(old, file))
                self.assertGreater(len(theirs), 0)
                pd.testing.assert_frame_equal(ours[list(theirs.columns)], theirs)
            self.assertEqual(read(os.path.join(old, "adaptation.csv")), read(os.path.join(new, "adaptation.csv")))

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
