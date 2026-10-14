# amber/split/session.py
"""Block-by-block sessions driven by amber-reader and amber-tag.

The C programs exchange one block per simulated millisecond over rfsim and pass every block
through these sessions. Messages (see wire.py):
  Reader -> Tags   {"t": "idle"} until every Tag has said hello, then {"t": "ms", "ms": n, "cmds": ...}
                   for n = 0 .. SIM_TIME_MS - 1, then {"t": "end"}
  Tag -> Reader    {"t": "hello"} while idle, {"t": "up", "ms": n, "up": ...} for each millisecond,
                   {"t": "bye"} after "end"
Every Tag message also carries "node". When the run is over each side writes its logs as CSV.
"""

import csv
import dataclasses
import os
from typing import Optional, Tuple

from . import wire
from .hosts import ReaderHost, TagHost, load_scenario


def _write_csv(path: str, records):
    if not records:
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[field.name for field in dataclasses.fields(records[0])])
        writer.writeheader()
        for record in records:
            writer.writerow(dataclasses.asdict(record))


class ReaderSession:
    def __init__(self, scenario_path: str, block_bytes: int, output_dir: str):
        self.host = ReaderHost(load_scenario(scenario_path))
        self.block_bytes = block_bytes
        self.output_dir = output_dir
        node_ids = self.host.scenario.NODE_IDS
        # rfsim adds up what all Tags write, so the Reader can only tell one Tag's message apart.
        if len(node_ids) != 1:
            raise ValueError(f"over rfsim the Reader can hear only one Tag; the scenario has {len(node_ids)} nodes")
        self._missing = set(node_ids)
        self._ms: Optional[int] = None
        self._ending = False

    def next_block(self) -> bytes:
        if self._ms is None:
            msg = {"t": "idle"}
        elif self._ms == self.host.scenario.SIM_TIME_MS:
            self._ending = True
            msg = {"t": "end"}
        else:
            msg = {"t": "ms", "ms": self._ms, "cmds": wire.encode_deliveries(self.host.run_ms(self._ms))}
        return wire.frame(msg, self.block_bytes)

    def on_block(self, block: bytes) -> bool:
        """Take the Tags' answer to the last block; True once the run is over."""
        msg = wire.unframe(block)
        if self._ms is None:
            if msg is not None and msg["t"] == "hello":
                if msg["node"] not in self.host.scenario.NODE_IDS:
                    raise ValueError(f"node {msg['node']} said hello but is not in the scenario")
                self._missing.discard(msg["node"])
                if not self._missing:
                    self._ms = 0
            return False
        if msg is None:
            raise RuntimeError(f"no answer from the Tag at {self._ms} ms")
        if self._ending:
            if msg["t"] != "bye":
                raise RuntimeError(f"expected bye from node {msg['node']}, got {msg['t']}")
            return True
        if msg["t"] != "up" or msg["ms"] != self._ms:
            raise RuntimeError(f"expected node {msg['node']}'s answer for {self._ms} ms, got {msg['t']} {msg.get('ms')}")
        self.host.apply_uplinks(self._ms, wire.decode_uplinks(msg["up"], msg["node"]))
        self._ms += 1
        return False

    def finish(self):
        for bs in self.host.bss:
            _write_csv(os.path.join(self.output_dir, f"reader_bs{bs.id}_rx.csv"), bs.rx_packets)
            _write_csv(os.path.join(self.output_dir, f"reader_bs{bs.id}_tx.csv"), bs.tx_packets)


class TagSession:
    def __init__(self, scenario_path: str, node_id: int, block_bytes: int, output_dir: str):
        self.host = TagHost(load_scenario(scenario_path), node_id)
        self.node_id = node_id
        self.block_bytes = block_bytes
        self.output_dir = output_dir
        self._next_ms = 0

    def reply(self, block: bytes) -> Tuple[bytes, bool]:
        """Answer the Reader's block; the flag is True once the run is over."""
        msg = wire.unframe(block)
        done = False
        if msg is None or msg["t"] == "idle":
            answer = {"t": "hello"}
        elif msg["t"] == "ms":
            ms = msg["ms"]
            if ms != self._next_ms:
                raise RuntimeError(f"node {self.node_id} expected {self._next_ms} ms, the Reader sent {ms} ms")
            self.host.deliver(ms, wire.decode_deliveries(msg["cmds"], self.node_id))
            answer = {"t": "up", "ms": ms, "up": wire.encode_uplinks(self.host.step(ms))}
            self._next_ms += 1
        elif msg["t"] == "end":
            answer = {"t": "bye"}
            done = True
        else:
            raise RuntimeError(f"unknown message {msg['t']} from the Reader")
        answer["node"] = self.node_id
        return wire.frame(answer, self.block_bytes), done

    def finish(self):
        bsm = self.host.bsm
        _write_csv(os.path.join(self.output_dir, f"tag{self.node_id}_tx.csv"), bsm.tx_records)
        _write_csv(os.path.join(self.output_dir, f"tag{self.node_id}_rx.csv"), bsm.rx_records)
