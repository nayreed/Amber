# amber/iq.py
"""IQ mode: rfsim carries real waveforms between the Reader and the Tags.

A scenario with a [phy] table runs in IQ mode. Then every rfsim block holds one millisecond of
IQ samples (amber.phy) and rfsim adds up what the Tags send, as normal rfsim does:

- The Reader's carrier is real and always on, for the whole run. A command keys it off for
  part of each chip, so a Tag harvests less while one is sent (its carrier_fraction). A Tag can
  only answer by reflecting the carrier.
- A command reaches a Tag only if the Tag decodes it from the waveform: commands are sent as
  bits (see encode_command), at phy.r2d_kbps, so a long command takes several milliseconds.
- A Tag's answer reaches the Reader only if the Reader decodes it from the sum of every Tag's
  reflection: two Tags reflecting at once really collide. Amber's collision window, SINR
  threshold and sensitivity checks are not used; the base station's enable_sic is.

A block is 2048 samples (8192 bytes): 1920 samples of signal (one millisecond at 1.92 Msps),
then 128 samples of control that are not on the air. The Reader's control says which
millisecond the block is ({idle, ms n, end}); each Tag marks its own place in the Tags' control
(hello, up, bye), so the Reader knows every Tag is there even though their signals add up.

run() runs a scenario in IQ mode on one machine, adding the Tags' blocks as rfsim does.
"""

import math
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import output, phy
from .hosts import ReaderHost, TagHost, _run_current_ms
from .proxies import Delivery

BLOCK_BYTES = 8192
BLOCK_SAMPLES = BLOCK_BYTES // 4
CONTROL_INT16 = 2 * (BLOCK_SAMPLES - phy.AIR_SAMPLES)  # 256 control values per block
MAX_TAGS = CONTROL_INT16

# Reader control: kind, then the millisecond in two 15-bit halves
IDLE, MS, END = 1, 2, 3
# A Tag's mark in the Tags' control
HELLO, UP, BYE = 1, 2, 3

BUILTIN_COMMANDS = ("send_id", "ack", "send_data")
CMD_BITS = 4
NODE_BITS = 16
COUNT_BITS = 8
SLOT_OFFSET_BITS = 11  # signed: a slot may start before the command has been decoded
SLOT_MS_BITS = 8


def check_block_bytes(block_bytes: int):
    if block_bytes != BLOCK_BYTES:
        raise ValueError(f"IQ mode needs amber.block_bytes = {BLOCK_BYTES}, got {block_bytes}")


def split_block(block: bytes) -> Tuple[np.ndarray, np.ndarray]:
    """(signal as interleaved int16, control int16) of one block."""
    v = np.frombuffer(block, dtype="<i2")
    return v[:2 * phy.AIR_SAMPLES], v[2 * phy.AIR_SAMPLES:]


def join_block(air: np.ndarray, control: np.ndarray) -> bytes:
    return np.concatenate([air.astype("<i2"), control.astype("<i2")]).tobytes()


def add_blocks(blocks: Sequence[bytes]) -> bytes:
    """The sum of the blocks, clipped at the 16-bit limits like rfsim's sum (a saturated ADC)."""
    total = np.zeros(BLOCK_SAMPLES * 2, dtype=np.int64)
    for b in blocks:
        total += np.frombuffer(b, dtype="<i2")
    return np.clip(total, -32768, 32767).astype("<i2").tobytes()


# ----------------------------------------------------------------------------------------------
# Commands as bits
# ----------------------------------------------------------------------------------------------

class CommandCodec:
    """Commands as bit fields, for the protocol a scenario runs.

    A command is: the command (4 bits, its index in commands), the target (2 bits: everyone, one
    Tag and its 16-bit id, or a count and that many ids), each integer field of COMMAND_FIELDS (a
    presence bit, then its value), and the receive slots it advertises (a presence bit, then their
    count; then where the first starts, in ms after the millisecond the command ends in, and their
    length). Slots must follow each other and be equally long, as every schedule here makes them.
    """

    def __init__(self, protocol: Any, node_ids: Sequence[int], bs_id: int):
        self.commands = list(dict.fromkeys(BUILTIN_COMMANDS + tuple(getattr(protocol, "COMMANDS", ()))))
        if len(self.commands) > 1 << CMD_BITS:
            raise ValueError(f"IQ mode: at most {1 << CMD_BITS} commands, the protocol has {len(self.commands)}")
        self.fields = dict(sorted(getattr(protocol, "COMMAND_FIELDS", {}).items()))
        self.node_ids = list(node_ids)
        self.bs_id = bs_id

    def encode(self, node_ids: Sequence[int], cmd: str, data: Dict[str, Any], end_ms: int) -> List[int]:
        if cmd not in self.commands:
            raise ValueError(f"IQ mode: command {cmd!r} is not one the protocol declares in COMMANDS")
        bits = phy.int_to_bits(self.commands.index(cmd), CMD_BITS)
        target = data.get("target", -1)
        if target == -1 and sorted(node_ids) == sorted(self.node_ids):
            bits += [0, 0]
        elif len(node_ids) == 1:
            bits += [0, 1] + phy.int_to_bits(node_ids[0], NODE_BITS)
        else:
            bits += [1, 0] + phy.int_to_bits(len(node_ids), COUNT_BITS)
            for n in node_ids:
                bits += phy.int_to_bits(n, NODE_BITS)
        for key in data:
            if key not in ("cmd", "target", "rx_slots") and key not in self.fields:
                raise ValueError(f"IQ mode: command {cmd!r} carries {key!r}, which has no bit width; "
                                 f"add it to the protocol's COMMAND_FIELDS")
        for key, width in self.fields.items():
            if key in data:
                bits += [1] + phy.int_to_bits(int(data[key]), width)
            else:
                bits += [0]
        if "rx_slots" not in data:
            return bits + [0]
        slots = [tuple(s) for s in data["rx_slots"]]
        bits += [1] + phy.int_to_bits(len(slots), COUNT_BITS)
        if slots:
            first, length = int(slots[0][0]), int(slots[0][1] - slots[0][0])
            for i, (a, b) in enumerate(slots):
                if (a, b) != (first + i * length, first + (i + 1) * length):
                    raise ValueError(f"IQ mode: the receive slots {slots} do not follow each other with one length")
            offset = first - end_ms
            bits += phy.int_to_bits(offset & ((1 << SLOT_OFFSET_BITS) - 1), SLOT_OFFSET_BITS)
            bits += phy.int_to_bits(length, SLOT_MS_BITS)
        return bits

    def decode(self, bits: Sequence[int], node_id: int, now_ms: int) -> Optional[Delivery]:
        """The command these bits hold, if it is for node_id; decoded in millisecond now_ms."""
        bits = list(bits)
        pos = 0

        def take(n: int) -> int:
            nonlocal pos
            if pos + n > len(bits):
                raise ValueError("command too short")
            v = phy.bits_to_int(bits[pos:pos + n])
            pos += n
            return v

        try:
            code = take(CMD_BITS)
            if code >= len(self.commands):
                return None
            cmd = self.commands[code]
            kind = take(2)
            if kind == 0:
                targets, target = None, -1
            elif kind == 1:
                target = take(NODE_BITS)
                targets = [target]
            elif kind == 2:
                targets = [take(NODE_BITS) for _ in range(take(COUNT_BITS))]
                target = targets
            else:
                return None
            data: Dict[str, Any] = {}
            for key, width in self.fields.items():
                if take(1):
                    data[key] = take(width)
            if take(1):
                count = take(COUNT_BITS)
                slots = []
                if count:
                    offset = take(SLOT_OFFSET_BITS)
                    if offset >= 1 << (SLOT_OFFSET_BITS - 1):
                        offset -= 1 << SLOT_OFFSET_BITS
                    length = take(SLOT_MS_BITS)
                    first = now_ms + offset
                    slots = [(float(first + i * length), float(first + (i + 1) * length)) for i in range(count)]
                data["rx_slots"] = slots
        except ValueError:
            return None
        if targets is not None and node_id not in targets:
            return None
        if cmd != "ack":
            data["cmd"] = cmd
            data["target"] = target
        return Delivery(node_id, cmd, self.bs_id, data)


def _group_deliveries(deliveries: Sequence[Delivery]) -> List[Tuple[List[int], str, Dict[str, Any]]]:
    """Consecutive deliveries of one command object, as BSBehavior makes for a broadcast, are
    one command on the air."""
    out: List[Tuple[List[int], str, Dict[str, Any]]] = []
    last = None
    for d in deliveries:
        if last is not None and last.cmd == d.cmd and last.data is d.data:
            out[-1][0].append(d.node_id)
        else:
            out.append(([d.node_id], d.cmd, d.data))
        last = d
    return out


def _up_scale(scenario: Any) -> float:
    """The Reader's receiver scale, which the Tags write their reflections at."""
    return phy.up_scale(phy.thermal_noise_w(scenario.reader_noise_figure_db))


def _seed(scenario: Any) -> int:
    return int(getattr(scenario, "SEED", 0)) & 0xFFFFFFFF


# ----------------------------------------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------------------------------------

class IqReaderSession:
    """The Reader in IQ mode: one block out and the sum of the Tags' blocks back, per millisecond."""

    def __init__(self, scenario: Any, block_bytes: int, output_dir: str):
        check_block_bytes(block_bytes)
        if len(scenario.NODE_IDS) > MAX_TAGS:
            raise ValueError(f"IQ mode runs at most {MAX_TAGS} Tags, the scenario has {len(scenario.NODE_IDS)}")
        self.scenario = scenario
        self.cfg: phy.PhyConfig = scenario.phy
        self.host = ReaderHost(scenario)
        self.output_dir = output_dir
        self.bs = self.host.bss[0]
        self.bs.phy_decides = True
        self.codec = CommandCodec(scenario.protocol, scenario.NODE_IDS, self.bs.id)
        self.links = scenario.iq_links()
        self.scale = _up_scale(scenario)
        self.tx = phy.ReaderTransmitter(self.cfg)
        self.rx = phy.ReaderReceiver(self.cfg, [sc for _, sc, _ in self.links.values()],
                                     phy.thermal_noise_w(scenario.reader_noise_figure_db), self.bs.enable_sic,
                                     np.random.default_rng([_seed(scenario), 0]), list(self.links))
        self._index = {node: i for i, node in enumerate(scenario.NODE_IDS)}
        self._ms: Optional[int] = None
        self._ending = False
        self.missed = 0  # frames decoded while the base station was not listening

    def next_block(self) -> bytes:
        control = np.zeros(CONTROL_INT16, dtype=np.int64)
        air = np.zeros(phy.AIR_SAMPLES, dtype=np.complex128)
        if self._ms is None:
            control[0] = IDLE
        elif self._ms == self.scenario.SIM_TIME_MS:
            self._ending = True
            control[0] = END
        else:
            ms = self._ms
            control[0], control[1], control[2] = MS, ms & 0x7FFF, ms >> 15
            for node_ids, cmd, data in _group_deliveries(self.host.run_ms(ms)):
                self._queue(node_ids, cmd, data, ms)
            air = self.tx.next_ms(True)
        return join_block(phy.to_counts(air, phy.TX_COUNTS), control)

    def _queue(self, node_ids: List[int], cmd: str, data: Dict[str, Any], ms: int):
        # The command's length does not depend on its slot offset, so its last millisecond is known
        # before the offset is filled in.
        probe = self.codec.encode(node_ids, cmd, data, ms)
        chips = len(phy.r2d_frame(probe))
        per_ms = phy.AIR_SAMPLES // self.cfg.r2d_samples_per_chip
        end_ms = ms + (self.tx.queued_chips + chips - 1) // per_ms
        self.tx.queue(self.codec.encode(node_ids, cmd, data, end_ms))

    def on_blocks(self, blocks: Sequence[bytes]) -> bool:
        """Take what the Tags sent back for the last block: their sum, as normal rfsim reads it,
        or each Tag's block, which are then added up here. True once the run is over."""
        air, control = split_block(add_blocks(blocks) if len(blocks) != 1 else blocks[0])
        marks = {node: int(control[i]) for node, i in self._index.items()}
        if self._ms is None:
            if all(m == HELLO for m in marks.values()):
                self._ms = 0
            return False
        expected = BYE if self._ending else UP
        wrong = [node for node, m in marks.items() if m != expected]
        if wrong:
            raise RuntimeError(f"nodes {wrong} did not answer as expected at {self._ms} ms")
        if self._ending:
            return True
        decoded, collisions = self.rx.receive(self._ms, phy.from_counts(air, self.scale))
        self._apply(decoded, collisions)
        self._ms += 1
        return False

    def _apply(self, decoded: List[phy.Decoded], collisions: int):
        """Hand what the Reader decoded to the base station, as a Tag's interrupt does."""
        bs = self.bs
        receptions = [(d.node_id, d.payload, d.rssi_dbm, self.links.get(d.node_id, (-1, 0))[0], d.subcarrier_shift)
                      for d in decoded if d.node_id in self.links]
        receptions += [(-1, None, -999.0, -1, 0)] * collisions
        for node_id, payload, rssi, sector, sc in receptions:
            if bs.mode != "rx":
                self.missed += 1
                continue
            bs.received_tag_id, bs.data_from_tag, bs.rssi_dbm = node_id, payload, rssi
            bs.best_sector_idx, bs.subcarrier_shift = sector, sc
            bs.action.interrupt("receive_data")
            _run_current_ms(self.host.env)
        if self.host._deliveries:
            raise ValueError(f"a base station sent a command while receiving at {self._ms} ms")

    def finish(self):
        output.write_reader_files(self.output_dir, getattr(self.scenario, "protocol", None), self.host.bss)


class IqTagSession:
    """One Tag in IQ mode: it receives the Reader's waveform and answers with its reflection."""

    def __init__(self, scenario: Any, node_id: int, block_bytes: int, output_dir: str):
        check_block_bytes(block_bytes)
        self.scenario = scenario
        self.cfg: phy.PhyConfig = scenario.phy
        self.host = TagHost(scenario, node_id)
        self.node_id = node_id
        self.output_dir = output_dir
        bsm = self.host.bsm
        bsm.phy_decides = True
        # A reply must end inside its slot, or the Reader is sending its next command by then
        bsm.tx_airtime_ms = math.ceil(self.cfg.d2r_frame_chips * self.cfg.d2r_samples_per_chip / phy.AIR_SAMPLES)
        self.node = bsm.node
        self.codec = CommandCodec(scenario.protocol, scenario.NODE_IDS, scenario.BS_IDS[0])
        rng = np.random.default_rng([_seed(scenario), 1, node_id])
        self.detector = phy.EnvelopeDetector(self.cfg, self.node.sensitivity_dbm, rng)
        self.reflector = phy.TagReflector(self.cfg, int(self.node.subcarrier_shift or 0), rng.uniform(0, 2 * math.pi))
        self.incident_w = bsm.downlink_results.get("best_pw_w", {}).get(node_id, 0.0)
        self.index = list(scenario.NODE_IDS).index(node_id)
        self.scale = _up_scale(scenario)
        self._next_ms = 0

    def reply(self, block: bytes) -> Tuple[bytes, bool]:
        air_in, control_in = split_block(block)
        kind = int(control_in[0])
        control = np.zeros(CONTROL_INT16, dtype=np.int64)
        air = np.zeros(phy.AIR_SAMPLES, dtype=np.complex128)
        done = False
        if kind == MS:
            ms = int(control_in[1]) | int(control_in[2]) << 15
            if ms != self._next_ms:
                raise RuntimeError(f"node {self.node_id} expected {self._next_ms} ms, the Reader sent {ms} ms")
            air = self._step(ms, phy.from_counts(air_in, phy.TX_COUNTS))
            self._next_ms += 1
            control[self.index] = UP
        elif kind == END:
            control[self.index] = BYE
            done = True
        else:  # idle, or nothing yet
            control[self.index] = HELLO
        return join_block(phy.to_counts(air, self.scale), control), done

    def _step(self, ms: int, carrier: np.ndarray) -> np.ndarray:
        self.node.carrier_fraction = float(np.mean(np.abs(carrier) ** 2))
        deliveries = []
        for bits in self.detector.receive(carrier * math.sqrt(self.incident_w)):
            d = self.codec.decode(bits, self.node_id, ms)
            if d is not None:
                deliveries.append(d)
        self.host.deliver(ms, deliveries)
        for up in self.host.step(ms):
            self.reflector.queue(self.node_id, up.payload, up.rssi_dbm)
        return self.reflector.next_ms(carrier, ms * phy.AIR_SAMPLES)

    def finish(self):
        output.write_tag_files(self.output_dir, self.node_id, self.host.bsm)


def run(scenario: Any, output_dir: str) -> Tuple[IqReaderSession, List[IqTagSession]]:
    """Run a scenario in IQ mode on one machine, adding the Tags' blocks as rfsim does, and write
    its files. Returns the sessions."""
    os.makedirs(output_dir, exist_ok=True)
    reader = IqReaderSession(scenario, BLOCK_BYTES, output_dir)
    tags = [IqTagSession(scenario, n, BLOCK_BYTES, output_dir) for n in scenario.NODE_IDS]
    done = False
    while not done:
        block = reader.next_block()
        answers = [t.reply(block)[0] for t in tags]
        done = reader.on_blocks([add_blocks(answers)])
    reader.finish()
    for t in tags:
        t.finish()
    return reader, tags
