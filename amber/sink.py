# amber/sink.py
"""The network's end of topology 2: the Tag data a UE that reads the Tags sends over its user plane.

    python -m amber.sink [--port 38391] [--host 0.0.0.0] [--output_dir DIR]

A UE whose Reader has a data destination (OAI's aiot.data_dest) sends each message it hears from
a Tag in one UDP datagram of Tag data (OAI's aiot_node_msg.h, amber.mac.parse_tag_data). The sink
listens where the network ends the UE's user plane (in OAI's topology 2 room, at the gNB, whose
tunnel is 10.0.1.1) and writes tag_data.csv: one row per message, in the columns of the UE's
reader_rx.csv, so the two compare row for row. A gap in seq is a lost message, logged and
counted; a datagram that is not Tag data is logged and dropped.

It writes each row as it comes, and runs until it is stopped (SIGTERM or SIGINT).
"""

import argparse
import csv
import math
import os
import signal
import socket
import sys
from typing import Optional, Tuple

from . import mac
from .iq import AIR_SAMPLES, read_sdu

PORT = 38391
COLUMNS = ("time_ms", "after", "kind", "node_id", "value", "id", "r_sfs", "slot", "rssi_dbfs")
FULL_SCALE2 = 32767.0 * 32767.0     # counts squared of a full-scale sample
SEQ_MOD = 1 << 32


def c_round(x: float) -> float:
    """x rounded half away from zero, as C's round() does (Python's round() goes to even)."""
    whole = float(math.trunc(x))
    if abs(x - whole) >= 0.5:       # x - whole is exact
        whole += math.copysign(1.0, x)
    return whole


def dbfs(counts2: float) -> float:
    """A power in counts squared, in dB of full scale to 0.01 dB, as the OAI Reader records it."""
    return c_round(100 * 10 * math.log10(counts2 / FULL_SCALE2)) / 100 if counts2 > 0 else -999.0


def row(m: mac.TagData) -> Tuple:
    """The reader_rx.csv row of a Tag data message."""
    a = m.answer
    node, value, ident = -1, -1, ""
    if a.kind == "msg1":
        ident = a.message.random_id
    else:
        v = read_sdu(a.message.sdu)
        if v is not None:
            node, value = v
        ident = a.as_id if a.as_id is not None else ""
    return (a.start / AIR_SAMPLES, mac.r2d_name(m.after), a.kind, node, value, ident, a.r_sfs, a.slot,
            dbfs(a.rssi_w))


def _log(text: str) -> None:
    print(f"amber.sink: {text}", file=sys.stderr, flush=True)


class Sink:
    """A UDP socket on (host, port) and the tag_data.csv it fills in output_dir."""

    def __init__(self, output_dir: str, host: str = "0.0.0.0", port: int = PORT):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        os.makedirs(output_dir, exist_ok=True)
        self.path = os.path.join(output_dir, "tag_data.csv")
        self._file = open(self.path, "w", newline="")
        self._csv = csv.writer(self._file)
        self._csv.writerow(COLUMNS)
        self._file.flush()
        self.received = 0       # Tag data messages written
        self.lost = 0           # the gaps in seq
        self.refused = 0        # datagrams that are not Tag data
        self._next: Optional[int] = None
        self._stop = False

    @property
    def address(self) -> Tuple[str, int]:
        return self.sock.getsockname()

    def take(self, data: bytes) -> bool:
        """One datagram: its row when it is Tag data. True when it was."""
        try:
            m = mac.parse_tag_data(data)
        except ValueError as e:
            self.refused += 1
            _log(f"dropped {e}")
            return False
        expected = 0 if self._next is None else self._next
        gap = (m.seq - expected) % SEQ_MOD
        if gap and gap < SEQ_MOD // 2:
            self.lost += gap
            _log(f"seq {expected} to {m.seq - 1} lost ({self.lost} so far)")
        elif gap:
            _log(f"seq {m.seq} after {expected - 1}: the sender started again, or the network reordered")
        self._next = (m.seq + 1) % SEQ_MOD
        r = row(m)
        self._csv.writerow(r)
        self._file.flush()
        self.received += 1
        if m.answer.kind == "data":
            ident = r[5] if r[5] != "" else "-"
            _log(f"data from Tag {r[3]} (AS ID {ident}): value {r[4]}, R_SFS {r[6]}, {r[8]:.1f} dBFS, after {r[1]}")
        return True

    def stop(self) -> None:
        self._stop = True

    def serve(self, poll_s: float = 0.2) -> None:
        """Take datagrams until stop()."""
        self.sock.settimeout(poll_s)
        while not self._stop:
            try:
                data = self.sock.recv(mac.NODE_MSG_MAX_BYTES + 1)
            except socket.timeout:
                continue
            except InterruptedError:
                continue
            self.take(data)

    def close(self) -> None:
        self.sock.close()
        self._file.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default: every one)")
    parser.add_argument("--port", type=int, default=PORT, help=f"UDP port (default: {PORT})")
    parser.add_argument("--output_dir", default=".", help="directory for tag_data.csv (default: here)")
    args = parser.parse_args(argv)
    sink = Sink(args.output_dir, args.host, args.port)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sink.stop())
    host, port = sink.address
    _log(f"listening on {host}:{port}, writing {sink.path}")
    try:
        sink.serve()
    finally:
        sink.close()
        _log(f"{sink.received} Tag data messages, {sink.lost} lost, {sink.refused} datagrams not Tag data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
