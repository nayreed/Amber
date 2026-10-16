# amber/protocols/gen2_q.py
"""EPC Gen2 style Q algorithm: the base station decides after every slot.

A round starts with a query command carrying Q. Each Tag loads a slot counter drawn uniformly
from 0 to 2^Q - 1. Every later frame of the round is a queryrep command, after which each Tag
counts its counter down by one; the Tag whose counter is 0 answers in the slot that follows.
A round has 2^Q slots, and every frame is one command of tx_ms and one slot of slot_ms, so the
policy runs again after every slot.

After each slot the base station keeps a floating value Qf (starting at q0): it adds c after a
slot with a collision, subtracts c after an empty slot, and leaves it after a success, within
[0, 15]. The next round uses Q = round(Qf). A slot holding both a decoded and a collided packet
counts as a collision, as in Gen2.

Unlike Gen2 there is no RN16 / ACK / EPC handshake: a Tag answers with its data once per round,
and every round asks every Tag again. The Tags start registered, so they sense data.

The base station keeps (time in s, Q, decoded, collided) for every slot in q_history; a run
writes it to q.csv.
"""

from .. import backscatter
from .base import Protocol


class Gen2Node(backscatter.BackscatterModule):
    """A Tag that counts down a slot counter loaded by query and answers when it reaches 0."""

    slot_counter = -1  # no round joined yet

    def handle_command(self, cmd, bs_id, data):
        if cmd == "query":
            self.slot_counter = self.rng.randint(0, 2 ** data["q"] - 1)
        elif cmd == "queryrep":
            if self.slot_counter >= 0:
                self.slot_counter -= 1
        else:
            return
        # Each command advertises the one slot that follows it.
        self.chosen_slot_idx = 0 if self.slot_counter == 0 and self.rx_slots else -1
        self.last_tx_command_time = self.env.now

    def build_tx_payload(self, sensor_data):
        return {"type": "data", "payload": sensor_data}


class Gen2Q(Protocol):
    PARAMS = {"q0": 4.0, "c": 0.3, "tx_ms": 1, "slot_ms": 10}
    TAG_CLASS = Gen2Node

    def check(self):
        self.positive("tx_ms", "slot_ms")
        p = self.params
        if not 0 <= p["q0"] <= 15:
            raise ValueError(f"protocol {self.name}: q0 must be within [0, 15], got {p['q0']}")
        if not 0 < p["c"] <= 1:
            raise ValueError(f"protocol {self.name}: c must be in (0, 1], got {p['c']}")

    def reader_args(self):
        return {"policy": self._policy}

    def setup_tag(self, bsm):
        bsm.state = "registered"

    def _policy(self, bs):
        p = self.params
        qf = float(p["q0"])
        bs.q_history = []
        while True:
            q = round(qf)
            for i in range(2 ** q):
                if i == 0:
                    command = {"cmd": "query", "target": -1, "q": q}
                else:
                    command = {"cmd": "queryrep", "target": -1}
                yield [("tx", p["tx_ms"], command["cmd"], command), ("rx", p["slot_ms"], "slot")]
                # Resumes after the slot; the frame had exactly one.
                slot = bs.slot_results[-1]
                if slot.collision:
                    qf = min(15.0, qf + p["c"])
                elif slot.empty:
                    qf = max(0.0, qf - p["c"])
                bs.q_history.append((bs.env.now / 1000.0, q, len(slot.decoded), slot.collided))

    def outputs(self, bss):
        columns = ("time_s", "q", "decoded", "collided")
        if len(bss) == 1:
            return {"q.csv": (columns, list(getattr(bss[0], "q_history", [])))}
        return {f"q_bs{bs.id}.csv": (columns, list(getattr(bs, "q_history", []))) for bs in bss}


PROTOCOL = Gen2Q
