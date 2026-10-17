# amber/protocols/adaptive_aloha.py
"""Adaptive framed-slotted ALOHA (EPC Gen2 Q-algorithm style), the protocol of the
demo_experiments/adaptive_aloha demo (formerly custom_protocol).

Each frame is a collect command to all Tags of tx_ms, then n listening slots of slot_ms; every
Tag that has energy answers in one slot it picks at random. After each frame the base station
counts the Tags it heard and the transmissions that collided, smooths that load
(load = (1 - smoothing) * load + smoothing * (heard + collided)), and moves n one slot towards
slots_per_contender * load, within [min_slots, max_slots] and with a one-slot dead band above
it. n starts at start_slots.

The base station keeps (time in s, n, heard, collided) for every frame in adapt_history; a run
writes it to adaptation.csv.
"""

from .. import backscatter
from .base import Protocol


class AdaptiveAlohaNode(backscatter.BackscatterModule):
    """A Tag that answers a collect command in a random slot of the advertised frame, with data
    it sensed (it becomes registered on its first collect, so its controller senses)."""

    def handle_command(self, cmd, bs_id, data):
        if cmd == "collect":
            # rx_slots holds the frame the base station advertised; with none, stay silent.
            self.chosen_slot_idx = self.rng.randint(0, len(self.rx_slots) - 1) if self.rx_slots else -1
            self.state = "registered"  # the controller senses data only for registered Tags
            self.last_tx_command_time = self.env.now

    def build_tx_payload(self, sensor_data):
        if self.state == "registered":
            return {"type": "data", "payload": sensor_data}
        return None


class AdaptiveAloha(Protocol):
    PARAMS = {"start_slots": 20, "min_slots": 2, "max_slots": 32, "tx_ms": 5, "slot_ms": 8,
              "smoothing": 0.25, "slots_per_contender": 2}
    TAG_CLASS = AdaptiveAlohaNode

    def check(self):
        self.positive("start_slots", "min_slots", "max_slots", "tx_ms", "slot_ms", "slots_per_contender")
        p = self.params
        if not p["min_slots"] <= p["start_slots"] <= p["max_slots"]:
            raise ValueError(f"protocol {self.name}: start_slots must be within [min_slots, max_slots], "
                             f"got {p['start_slots']} and [{p['min_slots']}, {p['max_slots']}]")
        if not 0 < p["smoothing"] <= 1:
            raise ValueError(f"protocol {self.name}: smoothing must be in (0, 1], got {p['smoothing']}")

    def reader_args(self):
        return {"policy": self._policy}

    def _policy(self, bs):
        p = self.params
        n_slots = p["start_slots"]
        prev_collided = 0  # bs.total_collided is cumulative
        load_ema = 0.0
        bs.adapt_history = []
        while True:
            # The command and its slots go in one frame, so the command can advertise the slots.
            yield [("tx", p["tx_ms"], "collect", {"cmd": "collect", "target": -1})] + \
                [("rx", p["slot_ms"], "slot") for _ in range(n_slots)]
            # Resumes once the whole frame has played out.
            heard = len(bs.decoded_this_frame)
            collided = bs.total_collided - prev_collided
            prev_collided = bs.total_collided
            bs.adapt_history.append((bs.env.now / 1000.0, n_slots, heard, collided))
            load_ema = (1 - p["smoothing"]) * load_ema + p["smoothing"] * (heard + collided)
            target = min(p["max_slots"], max(p["min_slots"], round(load_ema * p["slots_per_contender"])))
            if n_slots < target:
                n_slots += 1
            elif n_slots > target + 1:
                n_slots -= 1

    def outputs(self, bss):
        columns = ("time_s", "n_slots", "heard", "collided")
        if len(bss) == 1:
            return {"adaptation.csv": (columns, list(getattr(bss[0], "adapt_history", [])))}
        return {f"adaptation_bs{bs.id}.csv": (columns, list(getattr(bs, "adapt_history", []))) for bs in bss}


PROTOCOL = AdaptiveAloha
