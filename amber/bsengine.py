# amber/BSEngine.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Optional, List, Dict, Any, Tuple, Union
from collections import Counter
import numbers
import simpy

from .packet_analysis import apply_sic, thermal_noise_watts


@dataclass
class RxPacket:
    start_ms: float
    end_ms: float
    slot_id: str
    node_id: int
    payload: int
    rssi_dbm: float
    sector_idx: int
    expected_node: Union[int, str]  # node_id or "any"
    matched: bool  # Did node_id match expected?
    bs_id: int = 0  # Which BS received this packet
    subcarrier_shift: int = 0
    collided: bool = False  # True if collision (packet NOT decoded)


@dataclass
class TxPacket:
    start_ms: float
    end_ms: float
    slot_id: str
    target_node: int  # -1 = broadcast
    cmd: str
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SlotResult:
    """What the base station heard in one listening slot, empty slots included.

    A slot can hold both decoded and collided packets: without SIC the strongest of
    colliding packets may still be decoded (capture), and with SIC several may be.
    """
    slot_id: str
    start_ms: float
    end_ms: float
    decoded: List[int]  # node ids decoded in this slot
    collided: int  # packets in this slot that collided (not decoded)

    @property
    def empty(self) -> bool:
        """Nothing decoded and nothing collided: no packet above sensitivity arrived."""
        return not self.decoded and self.collided == 0

    @property
    def collision(self) -> bool:
        """At least one packet collided, whether or not another was decoded."""
        return self.collided > 0

    @property
    def success(self) -> bool:
        """At least one packet decoded and none collided."""
        return bool(self.decoded) and self.collided == 0


ScheduleEntry = Union[
    Tuple[str, int, str],        # (mode, duration_ms, slot_id)
    Tuple[str, int, str, Dict],  # (mode, duration_ms, slot_id, payload)
]


def whole_ms_frame(frame):
    """The frame with every duration as an int; ValueError naming the entry if one is not a whole
    number of milliseconds.

    The Reader and Tag containers advance one millisecond at a time and refuse anything in between,
    and Tags only transmit on their 1 ms tick, so a frame off that grid could not run on OAI and
    would be timed wrongly on one machine too. A duration such as 10.0 from float maths is fine.
    """
    out = []
    for entry in frame:
        duration = entry[1]
        whole = (isinstance(duration, numbers.Real) and not isinstance(duration, bool)
                 and float(duration).is_integer())
        if not whole:
            raise ValueError(f"frame entry {tuple(entry[:3])}: duration {duration!r} is not a whole number "
                             "of milliseconds; Amber durations must be whole milliseconds")
        out.append((entry[0], int(duration)) + tuple(entry[2:]))
    return out


class BSBehavior:
    """
    Schedule-driven base station.

    Carrier is always on (implicit) - transmitted continuously on a separate subcarrier
    for WPT. Nodes harvest energy all the time. The schedule only handles communication.

    Schedule entries (mode, duration_ms, slot_id, payload):

    - ("rx", 10, "listen"): Listen for any packets for 10ms
    - ("rx", 10, "poll_0", {"expect": 0}): Listen, expecting node 0
    - ("tx", 5, "cmd", {"target": -1, "cmd": "send_id"}): Broadcast send_id to all
    - ("tx", 5, "ack_slot", {"cmd": "ack"}): Send ack to all nodes in pending_ack queue
    - ("tx", 5, "cmd", {"target": -1, "cmd": "send_data"}): Request data from registered
    """

    def __init__(
        self,
        env: simpy.Environment,
        base_station,
        schedule: Optional[List[ScheduleEntry]] = None,
        backscatter_modules: List[Any] = None,  # List of BackscatterModule instances
        loop: bool = True,
        keep_logs: bool = True,  # if False, keep only counters and don't store packets in memory
        # SIC parameters
        required_sinr_db: float = 3.0,
        enable_sic: bool = True,
        cancellation_factor: float = 0.9,
        noise_figure_db: float = 6.0,
        bandwidth_hz: float = 100e6,
        # Callbacks
        on_rx: Optional[Callable[[RxPacket], None]] = None,
        on_tx: Optional[Callable[[TxPacket], None]] = None,
        # Protocol policy: a callable(bs) -> iterable of frames. If set, it
        # drives the base station instead of the static `schedule`.
        policy: Optional[Callable[["BSBehavior"], Any]] = None,
    ):
        self.env = env
        self.base_station = base_station
        self.id = base_station.id
        self.schedule = whole_ms_frame(schedule) if schedule else schedule
        self.policy = policy
        self.backscatter_modules = list(backscatter_modules) if backscatter_modules else []
        self.loop = loop
        self.on_rx = on_rx
        self.on_tx = on_tx

        # SIC parameters
        self.required_sinr_db = required_sinr_db
        self.enable_sic = enable_sic
        self.cancellation_factor = cancellation_factor
        self.noise_w = thermal_noise_watts(bandwidth_hz, noise_figure_db)

        # State
        self.mode = "idle"

        # Protocol state
        self.nodes_pending_ack: List[int] = []  # received ID, need to send ack
        self.nodes_registered: List[int] = []  # ack sent

        # Logs
        self.rx_packets: List[RxPacket] = []
        self.tx_packets: List[TxPacket] = []

        # If False, packets are counted but not stored.
        self.keep_logs: bool = keep_logs

        # Always-on aggregate counters (cheap, never grow with sim time)
        self.total_rx: int = 0
        self.total_collided: int = 0
        self.total_tx: int = 0
        self.rx_count_by_node: Counter = Counter()   # non-collided rx packets per node_id

        # Node ids decoded during the current frame; read by a policy to decide
        # how to answer (ack, register, adapt). Reset at the start of each frame.
        self.decoded_this_frame: List[int] = []
        # One SlotResult per listening slot of the current frame, empty slots included;
        # read by a policy after the frame. Reset at the start of each frame.
        self.slot_results: List[SlotResult] = []

        # Current RX window state
        self._rx_start_ms: float = 0.0
        self._rx_slot_id: str = ""
        self._rx_expect: Union[int, str] = "any"

        # Scratch (set by BackscatterModule before interrupt)
        self.received_tag_id: int = 0
        self.data_from_tag: int = 0
        self.rssi_dbm: float = -999.0
        self.best_sector_idx: int = -1
        self.subcarrier_shift: int = 0

        # IQ mode (amber.iq): the physics has already decided what was decoded. A reception with
        # node id -1 is a preamble heard but not decoded (a collision).
        self.phy_decides: bool = False

        # Buffer for collision detection (filled during RX window)
        self._rx_buffer: List[Dict[str, Any]] = []

        # Schedule tracking
        self._schedule_idx: int = 0  # Current position in schedule
        self._frame_start_ms: float = 0.0  # When current frame started

        # Pre-compute frame duration for lookahead
        self._frame_duration_ms: float = sum(e[1] for e in self.schedule) if self.schedule else 0.0

        self.action = env.process(self.run())

    def run(self):
        if self.policy is not None:
            yield from self._run_policy()
            return

        if not self.schedule:
            self.mode = "idle"
            while True:
                try:
                    yield self.env.timeout(10_000_000)
                except simpy.Interrupt:
                    pass

        while True:
            self._frame_start_ms = self.env.now
            self.decoded_this_frame = []
            self.slot_results = []
            for idx, entry in enumerate(self.schedule):
                self._schedule_idx = idx
                mode = entry[0]
                duration_ms = entry[1]
                slot_id = entry[2]
                payload = entry[3] if len(entry) > 3 else {}

                self.mode = mode

                if mode == "rx":
                    yield from self._do_rx(duration_ms, slot_id, payload)
                elif mode == "tx":
                    yield from self._do_tx(duration_ms, slot_id, payload)

            if not self.loop:
                self.mode = "idle"
                break

    def _run_policy(self):
        """Drive a user-supplied protocol policy instead of a static schedule.

        `self.policy` is a callable taking this BSBehavior and returning an
        iterable (typically a generator) of frames, where each frame is a list
        of schedule entries (mode, duration_ms, slot_id[, payload]). After each
        frame runs, the policy can read self.decoded_this_frame (node ids heard
        this frame), self.slot_results (what each listening slot of this frame held,
        empty slots included) and any other BS state to decide the next frame —
        this is where "ack or not", "register or not", and frame adaptation live.
        A frame of one command and one slot lets the policy decide after every slot.

        A command advertises the RX slots that follow it in the SAME frame, up
        to the frame's next TX entry (see _get_next_rx_slots).

        Every duration must be a whole number of milliseconds (see whole_ms_frame).
        """
        for frame in self.policy(self):
            if not frame:
                continue
            frame = whole_ms_frame(frame)
            # Reuse the static-schedule machinery (incl. lookahead) by making
            # the current frame the "schedule" for the duration of this frame.
            self.schedule = frame
            self._frame_duration_ms = sum(e[1] for e in frame)
            self._frame_start_ms = self.env.now
            self.decoded_this_frame = []
            self.slot_results = []
            for idx, entry in enumerate(frame):
                self._schedule_idx = idx
                mode = entry[0]
                duration_ms = entry[1]
                slot_id = entry[2]
                payload = entry[3] if len(entry) > 3 else {}
                self.mode = mode
                if mode == "rx":
                    yield from self._do_rx(duration_ms, slot_id, payload)
                elif mode == "tx":
                    yield from self._do_tx(duration_ms, slot_id, payload)
        self.mode = "idle"

    def _do_rx(self, duration_ms: int, slot_id: str, payload: Dict):
        """BS listening for uplink packets.

        Parameters
        ----------
        duration_ms : int
            Duration of this RX slot in milliseconds
        slot_id : str
            Identifier for this slot (for logging)
        payload : Dict
            Command payload, e.g. {"expect": 0} to expect node 0, or {"expect": "any"} to accept any node

        Yields
        ------
        simpy.events.Event
            Wait for duration of RX slot, but can be interrupted by incoming packets (handled by _buffer_rx)
        """        
        self._rx_start_ms = self.env.now
        self._rx_slot_id = slot_id
        self._rx_expect = payload.get("expect", "any")
        self._rx_buffer = []  # Clear buffer for new RX window

        end_time = self.env.now + duration_ms

        while self.env.now < end_time:
            remaining = end_time - self.env.now
            try:
                yield self.env.timeout(remaining)
            except simpy.Interrupt as it:
                if it.cause == "receive_data":
                    self._buffer_rx()

        # RX window ended - process buffer with collision detection
        decoded_before, collided_before = len(self.decoded_this_frame), self.total_collided
        self._process_rx_buffer()
        self.slot_results.append(SlotResult(
            slot_id=slot_id,
            start_ms=self._rx_start_ms,
            end_ms=self.env.now,
            decoded=self.decoded_this_frame[decoded_before:],
            collided=self.total_collided - collided_before,
        ))

    def _buffer_rx(self):
        """Buffer incoming transmission for later collision check."""
        self._rx_buffer.append({
            "time_ms": self.env.now,
            "node_id": self.received_tag_id,
            "payload": self.data_from_tag,
            "rssi_dbm": self.rssi_dbm,
            "sector_idx": self.best_sector_idx,
            "subcarrier_shift": self.subcarrier_shift,
        })

    def _process_rx_buffer(self, collision_window_ms: float = 5.0):
        """Process buffered receptions with SIC-based collision resolution.

        Parameters
        ----------
        collision_window_ms : float, optional
            Packets on same subcarrier within this time window collide, by default 5.0
        """        
        if not self._rx_buffer:
            return
        if self.phy_decides:
            for rx in self._rx_buffer:
                self._record_rx(rx, collided=rx["node_id"] < 0, check_sensitivity=False)
            return

        # Group by subcarrier_shift
        by_subcarrier: Dict[int, List[Dict]] = {}
        for rx in self._rx_buffer:
            sc = rx["subcarrier_shift"]
            if sc not in by_subcarrier:
                by_subcarrier[sc] = []
            by_subcarrier[sc].append(rx)

        # Process each subcarrier group
        for rx_list in by_subcarrier.values():
            # Sort by time
            rx_list.sort(key=lambda x: x["time_ms"])

            # Find collision groups (packets within collision_window_ms of each other)
            collision_groups: List[List[int]] = []
            used = set()

            for i, rx in enumerate(rx_list):
                if i in used:
                    continue

                # Find all packets that collide with this one
                group = [i]
                for j, other in enumerate(rx_list):
                    if j != i and j not in used:
                        if abs(rx["time_ms"] - other["time_ms"]) <= collision_window_ms:
                            group.append(j)

                if len(group) > 1:
                    # This is a collision group - apply SIC
                    collision_groups.append(group)
                    used.update(group)
                else:
                    # Single packet - no collision
                    used.add(i)

            # Process collision groups with SIC
            decoded_indices_global = set()
            for group in collision_groups:
                powers_dbm = [rx_list[i]["rssi_dbm"] for i in group]

                if self.enable_sic:
                    decoded_local, _ = apply_sic(
                        powers_dbm=powers_dbm,
                        noise_w=self.noise_w,
                        required_sinr_db=self.required_sinr_db,
                        cancellation_factor=self.cancellation_factor,
                    )
                else:
                    # Without SIC, only strongest can be decoded (capture effect)
                    decoded_local, _ = apply_sic(
                        powers_dbm=powers_dbm,
                        noise_w=self.noise_w,
                        required_sinr_db=self.required_sinr_db,
                        cancellation_factor=0.0,
                    )
                    decoded_local = decoded_local[:1] if decoded_local else []

                # Map local indices back to global
                for local_idx in decoded_local:
                    decoded_indices_global.add(group[local_idx])

            # Mark all packets as decoded or collided
            for i, rx in enumerate(rx_list):
                # Check if this packet was in a collision group
                in_collision = any(i in g for g in collision_groups)

                if in_collision:
                    collided = i not in decoded_indices_global
                else:
                    collided = False  # Single packet, no collision

                self._record_rx(rx, collided, check_sensitivity=True)

        # Buffer is now processed - acks will be sent in the next TX slot
        # nodes_pending_ack is populated here, cleared after acks are sent


    def _record_rx(self, rx: Dict[str, Any], collided: bool, check_sensitivity: bool):
        """Log one reception and, when decoded, act on it."""
        # Validate sector. A collision in IQ mode has none: the Reader cannot know who sent it.
        anonymous = collided and rx["node_id"] < 0
        if not anonymous:
            if rx["sector_idx"] < 0:
                print(f"Warning: Received packet with invalid sector index {rx['sector_idx']} from node {rx['node_id']}")
                return
            if rx["sector_idx"] >= len(self.base_station.sectors):
                print(f"Warning: Received packet with out-of-range sector index {rx['sector_idx']} from node {rx['node_id']}")
                return

            # Check sensitivity
            sector = self.base_station.sectors[rx["sector_idx"]]
            if check_sensitivity and rx["rssi_dbm"] < sector.sensitivity_dbm:
                return

        # Check if matched expected
        matched = (
            self._rx_expect == "any" or self._rx_expect == rx["node_id"]
        )

        pkt = RxPacket(
            start_ms=self._rx_start_ms,
            end_ms=rx["time_ms"],
            slot_id=self._rx_slot_id,
            node_id=rx["node_id"],
            payload=rx["payload"],
            rssi_dbm=rx["rssi_dbm"],
            sector_idx=rx["sector_idx"],
            expected_node=self._rx_expect,
            matched=matched,
            bs_id=self.id,
            subcarrier_shift=rx["subcarrier_shift"],
            collided=collided,
        )
        # counters (always maintained)
        self.total_rx += 1
        if collided:
            self.total_collided += 1
        else:
            self.rx_count_by_node[rx["node_id"]] += 1

        # keep in list (default), or discard (counters only)
        if self.keep_logs:
            self.rx_packets.append(pkt)

        # Only process decoded (non-collided) packets
        if not collided:
            self.decoded_this_frame.append(rx["node_id"])
            # Track node for ack if not already registered
            if rx["node_id"] not in self.nodes_registered:
                if rx["node_id"] not in self.nodes_pending_ack:
                    self.nodes_pending_ack.append(rx["node_id"])

            if self.on_rx:
                self.on_rx(pkt)

    def _get_next_rx_slots(self, start_after_ms: float) -> List[Tuple[float, float]]:
        """The RX slots a command sent now advertises: those after the current TX entry, up to the
        next TX entry.

        A TX entry sends a new command (or acks), which replaces the slots a node holds, so a slot
        after it could never be used by the node that picked it. A looping schedule continues into
        the next frame when the current one has no TX entry left; a policy frame or a schedule that
        does not loop ends with the frame.

        Parameters
        ----------
        start_after_ms : float
            Only include slots starting at or after this time

        Returns
        -------
        List[Tuple[float, float]]
            List of (start_ms, end_ms) tuples for upcoming RX slots
        """
        rx_slots = []
        schedule_len = len(self.schedule)
        if schedule_len == 0:
            return rx_slots
        wraps = self.loop and self.policy is None

        idx = self._schedule_idx
        frame_offset = 0  # how many full frames ahead idx is
        for _ in range(schedule_len - 1):
            idx += 1
            if idx == schedule_len:
                if not wraps:
                    break
                idx = 0
                frame_offset += 1
            mode, duration_ms = self.schedule[idx][0], self.schedule[idx][1]
            if mode == "tx":
                break
            time_in_frame = sum(self.schedule[i][1] for i in range(idx))
            slot_start = self._frame_start_ms + frame_offset * self._frame_duration_ms + time_in_frame
            if mode == "rx" and slot_start >= start_after_ms:
                rx_slots.append((slot_start, slot_start + duration_ms))
        return rx_slots

        # Start from current position in schedule
        idx = self._schedule_idx
        frame_offset = 0  # How many full frames ahead we are

        # Scan up to 2 full frames to find enough RX slots (handles edge cases)
        max_iterations = schedule_len * 2
        iterations = 0

        # while len(rx_slots) < num_slots and iterations < max_iterations:
        while iterations < max_iterations:
            iterations += 1

            # Move to next entry
            idx = (idx + 1) % schedule_len
            if idx == 0:
                frame_offset += 1

            entry = self.schedule[idx]
            mode = entry[0]
            duration_ms = entry[1]

            # Calculate absolute time for this entry
            # Time within frame for this entry
            time_in_frame = sum(self.schedule[i][1] for i in range(idx))
            slot_start = self._frame_start_ms + frame_offset * self._frame_duration_ms + time_in_frame
            slot_end = slot_start + duration_ms

            if mode == "rx" and slot_start >= start_after_ms:
                rx_slots.append((slot_start, slot_end))

        return rx_slots

    def _do_tx(self, duration_ms: int, slot_id: str, payload: Dict):
        """BS transmitting command/data to nodes.

        Parameters
        ----------
        duration_ms : int
            Duration of this TX slot in milliseconds
        slot_id : str
            Identifier for this slot (for logging)
        payload : Dict
            Command payload, e.g. {"target": -1, "cmd": "send_id"}

        Yields
        ------
        simpy.events.Event
            Wait for duration of TX slot, ignoring interrupts (half-duplex: can't receive while transmitting)
        """        
        start_ms = self.env.now
        cmd = payload.get("cmd", "")
        target = payload.get("target", -1)

        # If cmd is "ack", send pending acks
        if cmd == "ack":
            self._send_pending_acks()
        else:
            # Deliver command to backscatter modules
            self._deliver_command(cmd, target, payload, duration_ms)

        # Wait for TX duration, ignoring any receive_data interrupts
        # (nodes shouldn't transmit during BS TX, but handle gracefully)
        end_time = self.env.now + duration_ms
        while self.env.now < end_time:
            remaining = end_time - self.env.now
            try:
                yield self.env.timeout(remaining)
            except simpy.Interrupt:
                # Ignore interrupts during TX (half-duplex: can't receive while transmitting)
                pass

        end_ms = self.env.now
        pkt = TxPacket(
            start_ms=start_ms,
            end_ms=end_ms,
            slot_id=slot_id,
            target_node=target,
            cmd=cmd,
            data=payload,
        )
        # counters (always maintained)
        self.total_tx += 1

        # keep in list (default), or discard (counters only)
        if self.keep_logs:
            self.tx_packets.append(pkt)

        if self.on_tx:
            self.on_tx(pkt)

    def _deliver_command(self, cmd: str, target: Union[int, List[int]], payload: Dict, tx_duration_ms: int):
        """Deliver command to backscatter modules.

        Parameters
        ----------
        cmd : str
            Command string
        target : Union[int, List[int]]
            Target node ID(s), -1 for broadcast, single node ID, or list of node IDs
        payload : Dict
            Command payload
        tx_duration_ms : int
            Duration of TX slot (for slot timing calculation)
        """        
        cmd_data = dict(payload)

        # The RX slots up to the next command, after this TX slot
        rx_slots = self._get_next_rx_slots(self.env.now + tx_duration_ms)
        if target != -1:
            rx_slots = rx_slots[:1]
        cmd_data["rx_slots"] = rx_slots

        if target == -1:
            # Broadcast to all nodes
            for bsm in self.backscatter_modules:
                bsm.receive_command(cmd, self.id, cmd_data)
        elif isinstance(target, list):
            # Send to list of specific nodes
            for bsm in self.backscatter_modules:
                if bsm.node.id in target:
                    bsm.receive_command(cmd, self.id, cmd_data)
        else:
            # Send to single specific node
            for bsm in self.backscatter_modules:
                if bsm.node.id == target:
                    bsm.receive_command(cmd, self.id, cmd_data)

    def _send_pending_acks(self):
        """Send ack to all nodes that successfully sent their ID."""
        for node_id in list(self.nodes_pending_ack):
            # Find the backscatter module for this node
            for bsm in self.backscatter_modules:
                if bsm.node.id == node_id:
                    bsm.receive_command("ack", self.id, {})
                    break

            # Move from pending to registered
            self.nodes_pending_ack.remove(node_id)
            if node_id not in self.nodes_registered:
                self.nodes_registered.append(node_id)
