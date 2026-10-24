# amber/sink.py
"""The network's end of topology 2: the Tag data a UE that reads the Tags sends over its user plane.

    python -m amber.sink [--port 38391] [--host 0.0.0.0] [--output_dir DIR]

A UE whose Reader has a data destination (OAI's aiot.data_dest) sends each message it hears from
a Tag in one UDP datagram of Tag data (OAI's aiot_node_msg.h, amber.mac.parse_tag_data). The sink
listens where the network ends the UE's user plane (in OAI's topology 2 room, at the gNB, whose
tunnel is 10.0.1.1) and writes tag_data.csv: one row per message, in the columns of the UE's
reader_rx.csv, so the two compare row for row. A datagram that is not Tag data is logged and
dropped.

seq counts each sender's messages. The sink follows it per sender, from the first message it gets
(a sink that starts late counts nothing before it as lost): a gap is lost messages, a message that
comes late into a gap is written and no longer counted lost (reordered), and a message already
taken is a duplicate, counted and not written. A seq far behind the sender's newest means the
sender started again.

It appends each row as it comes to tag_data.csv (the header only when the file is new), and runs
until it is stopped (SIGTERM or SIGINT).
"""

import argparse
import csv
import logging
import math
import os
import signal
import socket
from typing import Dict, Optional, Tuple

from . import mac
from .iq import RX_COLUMNS, answer_row

PORT = 38391
COLUMNS = RX_COLUMNS[:-1] + ("rssi_dbfs",)
FULL_SCALE2 = 32767.0 * 32767.0     # counts squared of a full-scale sample
SEQ_MOD = 1 << 32
# How far behind a sender's newest seq a message still counts as late or duplicate, and so how
# many of the newest lost seqs are kept to take back a late one. Further behind is a restart.
SEQ_WINDOW = 4096

log = logging.getLogger("amber.sink")


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
    return answer_row(m.answer, mac.r2d_name(m.after)) + (dbfs(m.answer.rssi_w),)


class SeqTrack:
    """One sender's seq: what is lost, late or taken twice."""

    NEW, LATE, DUPLICATE, RESTART = "new", "late", "duplicate", "restart"

    def __init__(self, seq: int):
        self.next = (seq + 1) % SEQ_MOD
        self.missing: Dict[int, None] = {}      # lost seqs, oldest first, at most SEQ_WINDOW

    def take(self, seq: int) -> Tuple[str, int]:
        """(what seq is, how many seqs it shows lost)."""
        ahead = (seq - self.next) % SEQ_MOD
        if ahead < SEQ_MOD // 2:
            for s in range(seq - min(ahead, SEQ_WINDOW), seq):
                self.missing[s % SEQ_MOD] = None
            self.next = (seq + 1) % SEQ_MOD
            for s in list(self.missing):
                if (self.next - s) % SEQ_MOD <= SEQ_WINDOW:
                    break
                del self.missing[s]
            return self.NEW, ahead
        if seq in self.missing:
            del self.missing[seq]
            return self.LATE, 0
        if SEQ_MOD - ahead <= SEQ_WINDOW:
            return self.DUPLICATE, 0
        self.__init__(seq)
        return self.RESTART, 0


class Sink:
    """A UDP socket on (host, port) and the tag_data.csv it fills in output_dir."""

    def __init__(self, output_dir: str, host: str = "0.0.0.0", port: int = PORT):
        os.makedirs(output_dir, exist_ok=True)
        self.path = os.path.join(output_dir, "tag_data.csv")
        self._file = open(self.path, "a", newline="")
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.sock.bind((host, port))
            except OSError:
                self.sock.close()
                raise
        except OSError:
            self._file.close()
            raise
        self._csv = csv.writer(self._file)
        if self._file.tell() == 0:
            self._csv.writerow(COLUMNS)
            self._file.flush()
        self.received = 0       # Tag data messages written
        self.lost = 0           # seqs missing, less those that came late
        self.reordered = 0      # messages that came after a later seq, and were written
        self.duplicates = 0     # messages taken already, not written again
        self.refused = 0        # datagrams that are not Tag data
        self._senders: Dict[object, SeqTrack] = {}
        self._stop = False

    def __enter__(self) -> "Sink":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def address(self) -> Tuple[str, int]:
        return self.sock.getsockname()

    def take(self, data: bytes, sender: Optional[object] = None) -> bool:
        """One datagram from sender: its row when it is Tag data. True when it was."""
        try:
            m = mac.parse_tag_data(data)
        except ValueError as e:
            self.refused += 1
            log.warning("dropped %s", e)
            return False
        track = self._senders.get(sender)
        if track is None:
            self._senders[sender] = SeqTrack(m.seq)
            log.info("first message from %s: seq %d", sender, m.seq)
        else:
            what, gap = track.take(m.seq)
            if what == SeqTrack.DUPLICATE:
                self.duplicates += 1
                log.warning("seq %d from %s again: not written (%d duplicates so far)", m.seq, sender, self.duplicates)
                return True
            if what == SeqTrack.LATE:
                self.lost -= 1
                self.reordered += 1
                log.info("seq %d from %s came late (%d lost so far)", m.seq, sender, self.lost)
            elif what == SeqTrack.RESTART:
                log.warning("seq %d from %s, far behind: the sender started again", m.seq, sender)
            elif gap:
                self.lost += gap
                log.warning("seq %d to %d from %s lost (%d so far)", (m.seq - gap) % SEQ_MOD,
                            (m.seq - 1) % SEQ_MOD, sender, self.lost)
        r = row(m)
        self._csv.writerow(r)
        self._file.flush()
        self.received += 1
        a = m.answer
        if a.kind == "data":
            log.info("data from Tag %d (AS ID %s): value %d, R_SFS %d, %.1f dBFS, after %s",
                     r[3], "-" if a.as_id is None else a.as_id, r[4], a.r_sfs, r[-1], r[1])
        return True

    def stop(self) -> None:
        self._stop = True

    def serve(self, poll_s: float = 0.2) -> None:
        """Take datagrams until stop()."""
        self.sock.settimeout(poll_s)
        while not self._stop:
            try:
                data, sender = self.sock.recvfrom(mac.NODE_MSG_MAX_BYTES + 1)
            except socket.timeout:
                continue
            self.take(data, sender)

    def close(self) -> None:
        self.sock.close()
        self._file.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default: every one)")
    parser.add_argument("--port", type=int, default=PORT, help=f"UDP port (default: {PORT})")
    parser.add_argument("--output_dir", default=".", help="directory for tag_data.csv (default: here)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="amber.sink: %(message)s")
    with Sink(args.output_dir, args.host, args.port) as sink:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: sink.stop())
        host, port = sink.address
        log.info("listening on %s:%d, writing %s", host, port, sink.path)
        try:
            sink.serve()
        finally:
            log.info("%d Tag data messages, %d lost, %d late, %d duplicates, %d datagrams not Tag data",
                     sink.received, sink.lost, sink.reordered, sink.duplicates, sink.refused)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
