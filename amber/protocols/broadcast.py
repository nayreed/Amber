# amber/protocols/broadcast.py
"""Broadcast: the base station asks every Tag for data at once, then listens for a few slots.

Each frame is one send_data command to all Tags (target -1) of tx_ms, then `slots` listening
slots of rx_ms; each Tag that has energy answers in one slot it picks at random. The Tags start
registered, as send_data is only answered by registered Tags. This is the protocol of Amber's
broadcast and broadcast_sic demos.
"""

from .base import Protocol


class Broadcast(Protocol):
    PARAMS = {"slots": 2, "tx_ms": 5, "rx_ms": 10}

    def check(self):
        self.positive("slots", "tx_ms", "rx_ms")

    def reader_args(self):
        p = self.params
        schedule = [("tx", p["tx_ms"], "cmd", {"target": -1, "cmd": "send_data"})]
        schedule += [("rx", p["rx_ms"], "listen") for _ in range(p["slots"])]
        return {"schedule": schedule, "loop": True}

    def setup_reader(self, bs):
        bs.nodes_registered = list(self.node_ids)

    def setup_tag(self, bsm):
        bsm.state = "registered"


PROTOCOL = Broadcast
