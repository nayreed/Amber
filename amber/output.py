# amber/output.py
"""The CSV files Amber's demos write, from any run.

rx_packets.csv has every packet the base stations received, tx_records.csv every transmission
the Tags made, both sorted by time as the demos sort them; a protocol may add files of its own
(see Protocol.outputs). write_packets() writes the first two from the objects of a run in one
process; amber.split.export writes them from the logs of a split run.
"""

import os
from typing import Any, Iterable, List, Sequence

import pandas as pd

#: Columns of rx_packets.csv; time_ms is when the packet started.
RX_COLUMNS = ("bs_id", "time_ms", "node_id", "rssi_dbm", "expected_node", "collided", "matched", "subcarrier_shift")
#: Columns of tx_records.csv; time_ms is when the transmission ended.
TX_COLUMNS = ("node_id", "time_ms", "slot_idx", "payload_type", "delivered")


def rx_rows(bss) -> List[tuple]:
    """The rows of rx_packets.csv from the base stations (BSBehavior), in their order."""
    return [(bs.id, p.start_ms, p.node_id, p.rssi_dbm, p.expected_node, p.collided, p.matched, p.subcarrier_shift)
            for bs in bss for p in bs.rx_packets]


def tx_rows(bsms) -> List[tuple]:
    """The rows of tx_records.csv from the Tags (BackscatterModule), in their order."""
    return [(bsm.node.id, r.end_ms, r.slot_idx, r.payload_type, r.delivered) for bsm in bsms for r in bsm.tx_records]


def write_csv(path: str, columns: Sequence[str], rows: Iterable[Sequence[Any]], sort_by: str = None) -> None:
    """Write rows as the demos do, with pandas, sorted by one column if sort_by is given."""
    frame = pd.DataFrame(list(rows), columns=list(columns))
    if sort_by is not None:
        frame = frame.sort_values(sort_by)
    frame.to_csv(path, index=False)


def write_packet_rows(output_dir: str, rx: Iterable[Sequence[Any]], tx: Iterable[Sequence[Any]]) -> None:
    """Write rx_packets.csv and tx_records.csv from their rows."""
    write_csv(os.path.join(output_dir, "rx_packets.csv"), RX_COLUMNS, rx, sort_by="time_ms")
    write_csv(os.path.join(output_dir, "tx_records.csv"), TX_COLUMNS, tx, sort_by="time_ms")


def write_packets(output_dir: str, bss, bsms) -> None:
    """Write rx_packets.csv and tx_records.csv of a run in one process.

    Parameters
    ----------
    output_dir : str
        Directory to write to; it must exist.
    bss : list of BSBehavior
        The base stations, in the scenario's order.
    bsms : list of BackscatterModule
        The Tags, in the scenario's order.
    """
    write_packet_rows(output_dir, rx_rows(bss), tx_rows(bsms))


def write_protocol_outputs(output_dir: str, protocol, bss) -> None:
    """Write the files the protocol adds from its base stations (Protocol.outputs), if any."""
    for name, (columns, rows) in protocol.outputs(bss).items():
        write_csv(os.path.join(output_dir, name), columns, rows)
