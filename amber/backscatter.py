# amber/backscatter.py
"""A Tag's link to the Reader, between its energy (amber.controller) and its 38.391 MAC.

What a Tag hears and sends is fixed by 38.391 and runs in amber.phy and amber.mac (the device MAC and the
waveforms, driven by amber.iq). This module is what the Tag's Controller sees of it:

- powered: the Controller sets it every millisecond; a Tag that is off hears nothing and does not
  reflect (amber.iq passes it on to the link).
- sensed_value(): what the Tag sends, the value its Controller last sensed, whenever the MAC has it
  answer. The Tag never decides to transmit itself: it only answers the Reader, when 38.391 says.
- started_d2r(): amber.iq calls it when the Tag starts a D2R. It records the transmission (the
  Tag's tx file) and, if the Controller was waiting with data to send, puts it in its transmitting
  state, which draws the transmit current.
- heard_sdu(): records an R2D Data SDU the Tag received (the Tag's rx file).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

import simpy


@dataclass
class TxRecord:
    """One D2R the Tag started."""
    start_ms: float
    kind: str        # "msg1" (Access Random ID), or "msg2" / "cfa" / "data" (Upper Layer Data)
    payload: int     # the Random ID for msg1, else the sensed value sent
    r_sfs: int
    slot: int


@dataclass
class RxRecord:
    """One R2D Data SDU the Tag received for it."""
    end_ms: float
    sdu_hex: str


class AiotTag:
    """The Tag as its Controller drives it (see the module docstring)."""

    def __init__(self, env: simpy.Environment, node: Any, downlink_results: Optional[dict] = None):
        self.env = env
        self.node = node
        self.downlink_results = downlink_results or {}
        self.powered: bool = True       # the Controller sets it every millisecond
        self.controller = None          # set by the scenario once the Controller exists
        self.tx_records: List[TxRecord] = []
        self.rx_records: List[RxRecord] = []

    # -- what amber.iq calls ------------------------------------------------------------------
    def sensed_value(self) -> int:
        """The last value the Controller sensed and processed (0 until it has one)."""
        return int(getattr(self.controller, "processed_data", 0) or 0)

    def started_d2r(self, ms: int, send) -> None:
        payload = send.message.random_id if send.msg == "msg1" else self.sensed_value()
        self.tx_records.append(TxRecord(float(ms), send.msg, payload, send.params.r_sfs, send.slot))
        c = self.controller
        # A Tag waiting with data to send moves on to transmitting, which draws the transmit
        # current, as its duty cycle always did. A Tag still listening, sensing or processing
        # keeps doing so: restarting its cycle at every answer would stop it ever sensing.
        if c is not None and c.is_active and c.state_name == "wait_slot":
            c.state_name = "transmitting"
            c.counter = 0

    def heard_sdu(self, sdu: bytes) -> None:
        self.rx_records.append(RxRecord(float(self.env.now), sdu.hex()))
