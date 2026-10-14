# amber/protocols/unicast.py
"""Unicast: the base station asks each Tag for data in turn, one listening slot each.

Each frame is, for every Tag in the scenario's order, a send_data command addressed to it of
tx_ms, then one listening slot of rx_ms. The Tags start registered, as send_data is only answered
by registered Tags. This is the protocol of Amber's unicast demo.
"""

from .base import Protocol


class Unicast(Protocol):
    PARAMS = {"tx_ms": 5, "rx_ms": 10}

    def check(self):
        self.positive("tx_ms", "rx_ms")

    def reader_args(self):
        p = self.params
        schedule = [step for node_id in self.node_ids
                    for step in (("tx", p["tx_ms"], "cmd", {"target": node_id, "cmd": "send_data"}),
                                 ("rx", p["rx_ms"], "listen"))]
        return {"schedule": schedule, "loop": True}

    def setup_reader(self, bs):
        bs.nodes_registered = list(self.node_ids)

    def setup_tag(self, bsm):
        bsm.state = "registered"


PROTOCOL = Unicast
