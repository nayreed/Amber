# amber/split/export.py
"""Write rx_packets.csv and tx_records.csv, as Amber's demos write them, from the logs of a split
run: reader_bs<id>_rx.csv of each base station and tag<id>_tx.csv of each Tag.

    python3 -m amber.split.export SCENARIO LOG_DIR

The files go to LOG_DIR. They equal those amber.output.write_packets() writes for the same run in
one process: the rows are taken in the same order (base stations, then Tags, in the scenario's
order) with the same values.
"""

import csv
import os
import sys
from typing import Any, Dict, Iterator, List

from .. import output
from .hosts import load_scenario


def _value(text: str) -> Any:
    """A logged value back as the Python value it was written from."""
    for parse in (int, float):
        try:
            return parse(text)
        except ValueError:
            pass
    return {"True": True, "False": False}.get(text, text)


def _rows(path: str) -> Iterator[Dict[str, Any]]:
    """The rows of a log; none if it does not exist, as a log with no record is not written."""
    if not os.path.exists(path):
        return
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            yield {key: _value(text) for key, text in row.items()}


def run(scenario_path: str, log_dir: str) -> None:
    scenario = load_scenario(scenario_path)
    if not any(os.path.exists(os.path.join(log_dir, f"reader_bs{bs_id}_tx.csv")) for bs_id in scenario.BS_IDS):
        raise FileNotFoundError(f"no log of the Reader of {scenario_path} in {log_dir}")
    rx: List[tuple] = []
    for bs_id in scenario.BS_IDS:
        for r in _rows(os.path.join(log_dir, f"reader_bs{bs_id}_rx.csv")):
            rx.append((bs_id, r["start_ms"], r["node_id"], r["rssi_dbm"], r["expected_node"], r["collided"],
                       r["matched"], r["subcarrier_shift"]))
    tx: List[tuple] = []
    for node_id in scenario.NODE_IDS:
        for r in _rows(os.path.join(log_dir, f"tag{node_id}_tx.csv")):
            tx.append((node_id, r["end_ms"], r["slot_idx"], r["payload_type"], r["delivered"]))
    output.write_packet_rows(log_dir, rx, tx)


def main(argv) -> int:
    if len(argv) != 3:
        print("usage: python3 -m amber.split.export SCENARIO LOG_DIR", file=sys.stderr)
        return 2
    run(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
