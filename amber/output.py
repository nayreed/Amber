# amber/output.py
"""The CSV files of a run, and the one function that writes them.

The Reader writes:
  reader_tx.csv   every R2D message it sent: start and end (ms), the message and its main fields
  reader_rx.csv   every D2R it decoded, and every collision (a preamble found whose frame did not
                  decode): time (ms), the R2D it answered, kind (msg1 / data / collision), the
                  Tag's node id and value from the Data SDU, the Random ID or AS ID, R_SFS,
                  time slot and RSSI
plus its protocol's table, if it keeps one (Protocol.outputs): adaptation.csv or q.csv.
Each Tag writes:
  tag<id>_tx.csv  every D2R it started (amber.backscatter.TxRecord)
  tag<id>_rx.csv  every R2D Data SDU it received (RxRecord)

Every file is written even when it has no rows. The Reader and Tag containers on OAI each write
their own files; python -m amber.run writes all of them for a run on one machine, with the same
bytes for the same scenario and seed (amber.iq.run).
"""

import csv
import dataclasses
import os
from typing import Any, Iterable, Sequence

from .backscatter import RxRecord, TxRecord


def write_csv(path: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    """Write a header and rows; every CSV file of a run goes through here."""
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)


def _write_records(path: str, record_type, records) -> None:
    columns = [field.name for field in dataclasses.fields(record_type)]
    write_csv(path, columns, ([getattr(r, c) for c in columns] for r in records))


def write_reader_files(output_dir: str, protocol, record, tx_columns, rx_columns) -> None:
    """Write what the Reader sent and heard, and the files its protocol adds."""
    write_csv(os.path.join(output_dir, "reader_tx.csv"), tx_columns, record.tx)
    write_csv(os.path.join(output_dir, "reader_rx.csv"), rx_columns, record.rx)
    if protocol is not None:
        for name, (columns, rows) in protocol.outputs().items():
            write_csv(os.path.join(output_dir, name), columns, rows)


def write_tag_files(output_dir: str, node_id: int, bsm) -> None:
    """Write one Tag's transmissions and the Data SDUs it received."""
    _write_records(os.path.join(output_dir, f"tag{node_id}_tx.csv"), TxRecord, bsm.tx_records)
    _write_records(os.path.join(output_dir, f"tag{node_id}_rx.csv"), RxRecord, bsm.rx_records)
