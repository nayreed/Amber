# amber/split/session.py
"""Block-by-block sessions driven by amber-reader and amber-tag.

The C programs exchange one block per simulated millisecond over rfsim and pass every block
through these sessions: the Reader sends one block to all Tags and gets one block back from each
Tag. Messages (see wire.py):
  Reader -> Tags   {"t": "idle"} until every Tag has said hello, then {"t": "ms", "ms": n, "cmds": ...}
                   for n = 0 .. SIM_TIME_MS - 1, then {"t": "end"}
  Tag -> Reader    {"t": "hello"} while idle, {"t": "up", "ms": n, "up": ...} for each millisecond,
                   {"t": "bye"} after "end"
Every Tag message also carries "node". When the run is over each side writes its logs as CSV.
"""

import csv
import dataclasses
import os
from typing import Dict, List, Optional, Sequence, Tuple

from .. import output
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


def write_reader_logs(output_dir: str, bss):
    """Write each base station's received and sent packets as CSV."""
    for bs in bss:
        _write_csv(os.path.join(output_dir, f"reader_bs{bs.id}_rx.csv"), bs.rx_packets)
        _write_csv(os.path.join(output_dir, f"reader_bs{bs.id}_tx.csv"), bs.tx_packets)


def write_reader_outputs(output_dir: str, scenario, bss):
    """Write the files the scenario's protocol adds from its base stations, such as adaptation.csv."""
    protocol = getattr(scenario, "protocol", None)
    if protocol is not None:
        output.write_protocol_outputs(output_dir, protocol, bss)


def write_tag_logs(output_dir: str, node_id: int, bsm):
    """Write one node's transmissions and received commands as CSV."""
    _write_csv(os.path.join(output_dir, f"tag{node_id}_tx.csv"), bsm.tx_records)
    _write_csv(os.path.join(output_dir, f"tag{node_id}_rx.csv"), bsm.rx_records)


def _load_for_split(scenario_path: str):
    """The scenario, which must fix its seed: the Reader and every Tag must draw the same numbers."""
    scenario = load_scenario(scenario_path)
    if getattr(scenario, "seed_drawn", False):
        raise ValueError(f"{scenario_path}: a split run needs a seed in [run], the same for the Reader and every Tag")
    return scenario


class ReaderSession:
    def __init__(self, scenario_path: str, block_bytes: int, output_dir: str):
        self.host = ReaderHost(_load_for_split(scenario_path))
        self.block_bytes = block_bytes
        self.output_dir = output_dir
        self._node_ids = list(self.host.scenario.NODE_IDS)
        self._missing = set(self._node_ids)
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

    def on_blocks(self, blocks: Sequence[bytes]) -> bool:
        """Take the block each connected Tag answered the last block with; True once the run is over.

        Empty blocks, from a Tag that has just connected, are ignored. Once every node of the
        scenario has said hello, every node must answer every block exactly once."""
        by_node = self._by_node([msg for msg in map(wire.unframe, blocks) if msg is not None])
        if self._ms is None:
            self._missing -= {node for node, msg in by_node.items() if msg["t"] == "hello"}
            if not self._missing:
                self._ms = 0
            return False
        missing = [node for node in self._node_ids if node not in by_node]
        if missing:
            raise RuntimeError(f"no answer from nodes {missing} at {self._ms} ms")
        if self._ending:
            for node, msg in by_node.items():
                if msg["t"] != "bye":
                    raise RuntimeError(f"expected bye from node {node}, got {msg['t']}")
            return True
        uplinks = []
        for node in self._node_ids:
            msg = by_node[node]
            if msg["t"] != "up" or msg["ms"] != self._ms:
                raise RuntimeError(f"expected node {node}'s answer for {self._ms} ms, got {msg['t']} {msg.get('ms')}")
            uplinks += wire.decode_uplinks(msg["up"], node)
        self.host.apply_uplinks(self._ms, uplinks)
        self._ms += 1
        return False

    def _by_node(self, msgs: List[Dict]) -> Dict[int, Dict]:
        by_node: Dict[int, Dict] = {}
        for msg in msgs:
            node = msg["node"]
            if node not in self._node_ids:
                raise ValueError(f"node {node} answered but is not in the scenario")
            if node in by_node:
                raise RuntimeError(f"two Tags answered as node {node} at {self._ms} ms")
            by_node[node] = msg
        return by_node

    def finish(self):
        write_reader_logs(self.output_dir, self.host.bss)
        write_reader_outputs(self.output_dir, self.host.scenario, self.host.bss)


class TagSession:
    def __init__(self, scenario_path: str, node_id: int, block_bytes: int, output_dir: str):
        self.host = TagHost(_load_for_split(scenario_path), node_id)
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
        write_tag_logs(self.output_dir, self.node_id, self.host.bsm)
