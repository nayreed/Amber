# amber/backscatter.py
"""A Tag's link to the Reader, between its energy (amber.controller) and its 38.391 MAC.

What a Tag hears and sends is fixed by 38.391 and runs in amber.phy and amber.mac (the device MAC and the
waveforms, driven by amber.iq). This module is what the Tag's Controller sees of it:

- powered: the Controller sets it every millisecond; a Tag that is off hears nothing and does not
  reflect (amber.iq passes it on to the link).
- state is always "registered", so the Controller keeps sensing: the Tag sends its last sensed
  value whenever the MAC has it answer.
- started_d2r(): amber.iq calls it when the Tag starts a D2R. It records the transmission and, if
  the Controller was waiting with data to send, puts it in its transmitting state, which draws the
  transmit current.
- controller_tx_ready() never asks to transmit: a Tag only answers the Reader, when 38.391 says.
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

    def __init__(self, env: simpy.Environment, node: Any, uplink_results: Optional[dict] = None,
                 downlink_results: Optional[dict] = None, keep_logs: bool = True, **_ignored):
        self.env = env
        self.node = node
        self.uplink_results = uplink_results or {}
        self.downlink_results = downlink_results or {}
        self.keep_logs = keep_logs
        self.state = "registered"
        self.powered: bool = True       # the Controller sets it every millisecond
        self.controller = None          # set by the scenario once the Controller exists
        self.tx_records: List[TxRecord] = []
        self.rx_records: List[RxRecord] = []
        self.packets_sent = 0

    # -- what the Controller calls -------------------------------------------------------------
    def controller_tx_ready(self, sensor_data: int):
        return None

    def do_transmit(self, tx_info) -> bool:   # never called: controller_tx_ready() returns None
        return False

    # -- what amber.iq calls ------------------------------------------------------------------
    def sensed_value(self) -> int:
        """The last value the Controller sensed and processed (0 until it has one)."""
        return int(getattr(self.controller, "processed_data", 0) or 0)

    def started_d2r(self, ms: int, send) -> None:
        payload = send.message.random_id if send.msg == "msg1" else self.sensed_value()
        if self.keep_logs:
            self.tx_records.append(TxRecord(float(ms), send.msg, payload, send.params.r_sfs, send.slot))
        self.packets_sent += 1
        c = self.controller
        # A Tag waiting with data to send moves on to transmitting, which draws the transmit
        # current, as its duty cycle always did. A Tag still listening, sensing or processing
        # keeps doing so: restarting its cycle at every answer would stop it ever sensing.
        if c is not None and c.is_active and c.state_name == "wait_slot":
            c.state_name = "transmitting"
            c.counter = 0

    def heard_sdu(self, sdu: bytes) -> None:
        if self.keep_logs:
            self.rx_records.append(RxRecord(float(self.env.now), sdu.hex()))
