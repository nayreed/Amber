# amber/output.py
"""The CSV files of a run, and the one function that writes them.

The Reader writes, for each base station:
  reader_bs<id>_rx.csv   every packet it received (RxPacket), collided ones included
  reader_bs<id>_tx.csv   every command it sent (TxPacket)
plus the files its protocol adds (Protocol.outputs), such as adaptation.csv or q.csv.
Each Tag writes:
  tag<id>_tx.csv         every transmission it made (TxRecord)
  tag<id>_rx.csv         every command it heard (RxRecord)

Every file is written even when it has no rows, so a run in which nothing was received has a
reader_bs<id>_rx.csv holding only its header. The Reader and Tag containers on OAI each write
their own files; python -m amber.run writes all of them for a run on one machine, with the same
bytes for the same scenario and seed.
"""

import csv
import dataclasses
import os
from typing import Any, Iterable, Sequence

from .backscatter import RxRecord, TxRecord
from .bsengine import RxPacket, TxPacket


def write_csv(path: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    """Write a header and rows; every CSV file of a run goes through here."""
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)


def _write_records(path: str, record_type, records) -> None:
    columns = [field.name for field in dataclasses.fields(record_type)]
    write_csv(path, columns, ([getattr(r, c) for c in columns] for r in records))


def write_reader_files(output_dir: str, protocol, bss) -> None:
    """Write each base station's received and sent packets, and the files its protocol adds."""
    for bs in bss:
        _write_records(os.path.join(output_dir, f"reader_bs{bs.id}_rx.csv"), RxPacket, bs.rx_packets)
        _write_records(os.path.join(output_dir, f"reader_bs{bs.id}_tx.csv"), TxPacket, bs.tx_packets)
    if protocol is not None:
        for name, (columns, rows) in protocol.outputs(bss).items():
            write_csv(os.path.join(output_dir, name), columns, rows)


def write_tag_files(output_dir: str, node_id: int, bsm) -> None:
    """Write one Tag's transmissions and the commands it heard."""
    _write_records(os.path.join(output_dir, f"tag{node_id}_tx.csv"), TxRecord, bsm.tx_records)
    _write_records(os.path.join(output_dir, f"tag{node_id}_rx.csv"), RxRecord, bsm.rx_records)


def write_run(output_dir: str, scenario, bss, bsms) -> None:
    """Write every file of a run on one machine: the Reader's, then each Tag's."""
    os.makedirs(output_dir, exist_ok=True)
    write_reader_files(output_dir, getattr(scenario, "protocol", None), bss)
    for node_id, bsm in zip(scenario.NODE_IDS, bsms):
        write_tag_files(output_dir, node_id, bsm)
