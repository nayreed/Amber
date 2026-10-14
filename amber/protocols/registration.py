# amber/protocols/registration.py
"""Registration: Amber's built-in Tag protocol from the start, with no Tag registered.

Each frame is a send_id command to all Tags of tx_ms and `id_slots` listening slots of rx_ms, in
which every unregistered Tag answers with its id in a random slot; then an ack command of tx_ms
to the Tags heard, which registers them; then a send_data command to all Tags of tx_ms and
`data_slots` listening slots of rx_ms, in which the registered Tags answer with data.
"""

from .base import Protocol


class Registration(Protocol):
    PARAMS = {"id_slots": 3, "data_slots": 2, "tx_ms": 5, "rx_ms": 10}

    def check(self):
        self.positive("id_slots", "data_slots", "tx_ms", "rx_ms")

    def reader_args(self):
        p = self.params
        schedule = [("tx", p["tx_ms"], "cmd", {"target": -1, "cmd": "send_id"})]
        schedule += [("rx", p["rx_ms"], "listen") for _ in range(p["id_slots"])]
        schedule += [("tx", p["tx_ms"], "ack_slot", {"cmd": "ack"}),
                     ("tx", p["tx_ms"], "cmd", {"target": -1, "cmd": "send_data"})]
        schedule += [("rx", p["rx_ms"], "listen") for _ in range(p["data_slots"])]
        return {"schedule": schedule, "loop": True}


PROTOCOL = Registration
