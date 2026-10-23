# amber/iq.py
"""rfsim carries real Rel-19 A-IoT waveforms between the Reader and the Tags.

Every rfsim block holds one millisecond of IQ samples at 5.76 Msps (amber.phy, amber.mac), and rfsim adds
up what the Tags send, as normal rfsim does:

- The Reader's carrier is always on. An R2D message (38.391) is the carrier keyed by OOK chips in
  OFDM symbols (38.291 §6.2), so a Tag harvests less while one is sent (its carrier_fraction).
- A Tag hears an R2D message only if it decodes it from the waveform, runs the 38.391 device MAC
  on it, and answers by reflecting the carrier (38.291 §6.1) at T_R->D after the R2D's end.
- The Reader opens a window for every answer an R2D message can cause and decodes what arrives
  from the sum of every Tag's reflection: two Tags on one access occasion and R_SFS really
  collide. Whether the Reader cancels the stronger first is the base station's enable_sic.

What the Reader sends is its protocol's choice (amber.protocols), run by the A-IoT MAC library
(amber.mac): the same C code a gNB or a UE runs as the Reader. The Tag side is the same for every
protocol: 38.391 fixes it. A Tag is on, and can hear and answer, only while its capacitor and controller
say so (amber.controller); its answer is its node id and its last sensed value as the Data SDU.

A block is 5888 samples (23552 bytes): 5760 samples of signal (one millisecond at 5.76 Msps),
then 128 samples of control that are not on the air. The Reader's control says which millisecond
the block is ({idle, ms n, end}); each Tag marks its own place in the Tags' control (hello, up,
bye), so the Reader knows every Tag is there even though their signals add up.

An air-only block (AIR_BLOCK_BYTES, 5760 samples) has no control. It is for rfsim_hub (OAI's
radio/rfsimulator/hub), where the Reader and the Tags are all clients: the hub starts everyone
together at sample 0, block n is millisecond n, and both sides stop after the scenario's SIM_TIME_MS.
On air-only blocks the Reader's and every Tag's files are the same as with control blocks.

A carrier node (IqCarrierSession) sends only the carrier, for a room whose [network] has the carrier
come from a node of its own (cw = "cw-node-dl" or "cw-node-ul") through rfsim_hub.

run() runs a scenario on one machine, adding the Tags' blocks as rfsim does.
"""

import math
import os
import struct
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np

from . import link as rlink
from . import mac, output
from .hosts import TagHost
from .link import LinkConfig
from .phy import params as rparams

AIR_SAMPLES = rparams.SAMPLES_PER_MS              # 5760
CONTROL_SAMPLES = 128
BLOCK_SAMPLES = AIR_SAMPLES + CONTROL_SAMPLES
BLOCK_BYTES = BLOCK_SAMPLES * 4                   # int16 I and Q
AIR_BLOCK_BYTES = AIR_SAMPLES * 4                 # air only, no control: through rfsim_hub
CONTROL_INT16 = 2 * CONTROL_SAMPLES               # 256 control values per block
MAX_TAGS = CONTROL_INT16

# Reader control: kind, then the millisecond in two 15-bit halves
IDLE, MS, END = 1, 2, 3
# A Tag's mark in the Tags' control
HELLO, UP, BYE = 1, 2, 3

# rfsim carries 16-bit samples. The Reader sends its carrier at TX_COUNTS; a Tag scales it by the
# power it receives. What reaches the Reader is written as its 16-bit receiver would sample it,
# with the thermal noise at ADC_NOISE_COUNTS rms on I and on Q (see up_scale).
TX_COUNTS = 8192
ADC_NOISE_COUNTS = 1.0
K_BOLTZMANN = 1.380649e-23
T_REF_K = 290.0


def dbm_to_w(dbm: float) -> float:
    return 10.0 ** ((dbm - 30.0) / 10.0)


def w_to_dbm(w: float) -> float:
    return 10.0 * math.log10(w) + 30.0 if w > 0 else -999.0


def thermal_noise_w(noise_figure_db: float) -> float:
    """Thermal noise over the 5.76 MHz the samples cover."""
    return K_BOLTZMANN * T_REF_K * rparams.SAMPLE_RATE_HZ * 10.0 ** (noise_figure_db / 10.0)


def to_counts(x: np.ndarray, scale: float) -> np.ndarray:
    """Complex signal times scale as interleaved int16 (I, Q), rounded and clipped."""
    out = np.empty(2 * len(x), dtype=np.float64)
    out[0::2] = x.real * scale
    out[1::2] = x.imag * scale
    return np.clip(np.rint(out), -32768, 32767).astype(np.int16)


def from_counts(iq: np.ndarray, scale: float) -> np.ndarray:
    """Interleaved int16 (I, Q) divided by scale, as complex."""
    iq = iq.astype(np.float64)
    return (iq[0::2] + 1j * iq[1::2]) / scale


def up_scale(noise_w: float) -> float:
    """Counts per sqrt(watt) of the Reader's receiver, given its thermal noise power."""
    return ADC_NOISE_COUNTS / math.sqrt(noise_w / 2)


def check_block_bytes(block_bytes: int) -> bool:
    """True if the blocks carry control (BLOCK_BYTES), False if they are air only (AIR_BLOCK_BYTES)."""
    if block_bytes not in (BLOCK_BYTES, AIR_BLOCK_BYTES):
        raise ValueError(f"IQ needs amber.block_bytes = {BLOCK_BYTES} (with control) or {AIR_BLOCK_BYTES} (air only), "
                         f"got {block_bytes}")
    return block_bytes == BLOCK_BYTES


def split_block(block: bytes) -> Tuple[np.ndarray, np.ndarray]:
    """(signal as interleaved int16, control int16) of one block."""
    v = np.frombuffer(block, dtype="<i2")
    return v[:2 * AIR_SAMPLES], v[2 * AIR_SAMPLES:]


def join_block(air: np.ndarray, control: np.ndarray) -> bytes:
    return np.concatenate([air.astype("<i2"), control.astype("<i2")]).tobytes()


def add_blocks(blocks: Sequence[bytes]) -> bytes:
    """The sum of the blocks, clipped at the 16-bit limits like rfsim's sum (a saturated ADC)."""
    total = np.zeros(len(blocks[0]) // 2, dtype=np.int64)
    for b in blocks:
        total += np.frombuffer(b, dtype="<i2")
    return np.clip(total, -32768, 32767).astype("<i2").tobytes()


def _seed(scenario: Any) -> int:
    return int(getattr(scenario, "SEED", 0)) & 0xFFFFFFFF


# ----------------------------------------------------------------------------------------------
# What the Tags send up: the Data SDU
# ----------------------------------------------------------------------------------------------

SDU_FORMAT = ">HB"      # node id (16 bits), last sensed value (8 bits; 0 = nothing sensed yet)


def make_sdu(node_id: int, value: int) -> bytes:
    return struct.pack(SDU_FORMAT, node_id & 0xFFFF, max(0, min(255, int(value))))


def read_sdu(sdu: bytes) -> Optional[Tuple[int, int]]:
    """(node id, value) of a Tag's Data SDU, or None if it is not one."""
    if len(sdu) != struct.calcsize(SDU_FORMAT):
        return None
    return struct.unpack(SDU_FORMAT, sdu)


def paging_id(node_id: int) -> List[int]:
    """The Paging ID a Tag answers to: its node id on 16 bits (38.391 leaves it to upper layers)."""
    return [(node_id >> (15 - i)) & 1 for i in range(16)]


# ----------------------------------------------------------------------------------------------
# The Reader
# ----------------------------------------------------------------------------------------------

@dataclass
class ReaderRecord:
    """Everything the Reader sent and heard, for its CSV files."""
    tx: List[Tuple] = field(default_factory=list)
    rx: List[Tuple] = field(default_factory=list)


TX_COLUMNS = ("start_ms", "end_ms", "message", "detail")
RX_COLUMNS = ("time_ms", "after", "kind", "node_id", "value", "id", "r_sfs", "slot", "rssi_dbm")


class IqReaderSession:
    """The Reader: one block out and the sum of the Tags' blocks back, per millisecond."""

    def __init__(self, scenario: Any, block_bytes: int, output_dir: str):
        self.control = check_block_bytes(block_bytes)
        if self.control and len(scenario.NODE_IDS) > MAX_TAGS:
            raise ValueError(f"IQ runs at most {MAX_TAGS} Tags, the scenario has {len(scenario.NODE_IDS)}")
        self.scenario = scenario
        self.cfg: LinkConfig = scenario.phy
        self.output_dir = output_dir
        self.protocol = scenario.protocol
        self.noise_w = thermal_noise_w(scenario.reader_noise_figure_db)
        self.scale = up_scale(self.noise_w)
        leak_dbm, pn_floor, pn_corner = scenario.cw_leak
        leak = rlink.CarrierLeak(dbm_to_w(leak_dbm), pn_floor, pn_corner, np.random.default_rng([_seed(scenario), 1]))
        self.record = ReaderRecord()
        self.link = rlink.ReaderLink(self.protocol.reader(self.cfg), self.cfg.m_chip_symb, self.cfg.rb, self.noise_w,
                                     scenario.enable_sic, np.random.default_rng([_seed(scenario), 0]), leak,
                                     on_heard=self._log)
        self._index = {node: i for i, node in enumerate(scenario.NODE_IDS)}
        self._ms: Optional[int] = None
        self._ending = False

    def _log(self, h):
        name, detail = mac.describe(h.sent)
        self.record.tx.append((h.r2d_start / AIR_SAMPLES, h.r2d_end / AIR_SAMPLES, name, detail))
        for a in h.answers:
            node, value, ident = -1, -1, ""
            if a.kind == "msg1":
                ident = a.message.random_id
            else:
                v = read_sdu(a.message.sdu)
                if v is not None:
                    node, value = v
                ident = a.as_id if a.as_id is not None else ""
            self.record.rx.append((a.start / AIR_SAMPLES, name, a.kind, node, value, ident, a.r_sfs, a.slot,
                                   round(w_to_dbm(a.rssi_w), 2)))
        for _ in range(h.collided):
            self.record.rx.append((h.r2d_end / AIR_SAMPLES, name, "collision", -1, -1, "", "", "", ""))

    def next_block(self) -> bytes:
        if not self.control:  # the hub has started every Tag with the Reader: millisecond n is block n
            if self._ms is None:
                self._ms = 0
            return to_counts(self.link.next_block(), TX_COUNTS).astype("<i2").tobytes()
        control = np.zeros(CONTROL_INT16, dtype=np.int64)
        air = np.zeros(AIR_SAMPLES, dtype=np.complex128)
        if self._ms is None:
            control[0] = IDLE
        elif self._ms == self.scenario.SIM_TIME_MS:
            self._ending = True
            control[0] = END
        else:
            ms = self._ms
            control[0], control[1], control[2] = MS, ms & 0x7FFF, ms >> 15
            air = self.link.next_block()
        return join_block(to_counts(air, TX_COUNTS), control)

    def on_block(self, block: bytes) -> bool:
        """Take what the Tags sent back for the last block: the sum of their signals, as rfsim
        reads it. True once the run is over."""
        if not self.control:
            self.link.receive(from_counts(np.frombuffer(block, dtype="<i2"), self.scale))
            self._ms += 1
            return self._ms == self.scenario.SIM_TIME_MS
        air, control = split_block(block)
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
        self.link.receive(from_counts(air, self.scale))
        self._ms += 1
        return False

    def finish(self):
        output.write_reader_files(self.output_dir, self.protocol, self.record, TX_COLUMNS, RX_COLUMNS)


# ----------------------------------------------------------------------------------------------
# A carrier node
# ----------------------------------------------------------------------------------------------

# The [network] carriers that come from a node of their own, outside the Reader (TR 38.769 6.8.1).
CARRIER_NODES = ("cw-node-dl", "cw-node-ul")


class IqCarrierSession:
    """A carrier node: it sends only the carrier the Tags reflect, unmodulated, at TX_COUNTS, so a
    Tag receives it at the scenario's incident power, as it would Amber's Reader's carrier. It is for
    a scenario whose [network] cw is a carrier node, in an rfsim_hub room (air-only blocks): the hub
    puts the carrier where the network has it. What the node hears is not used."""

    def __init__(self, scenario: Any, block_bytes: int, output_dir: str):
        if check_block_bytes(block_bytes):
            raise ValueError(f"a carrier node runs in an rfsim_hub room, with air-only blocks of {AIR_BLOCK_BYTES} bytes")
        if scenario.network.cw not in CARRIER_NODES:
            raise ValueError(f"the scenario's carrier is {scenario.network.cw!r}, not a carrier node {CARRIER_NODES}")
        self.scenario = scenario
        self.output_dir = output_dir
        self._block = to_counts(np.ones(AIR_SAMPLES, dtype=np.complex128), TX_COUNTS).astype("<i2").tobytes()
        self._ms = 0

    def next_block(self) -> bytes:
        return self._block

    def on_block(self, block: bytes) -> bool:
        """True once the scenario's SIM_TIME_MS have been sent."""
        self._ms += 1
        return self._ms == self.scenario.SIM_TIME_MS

    def finish(self):
        pass


# ----------------------------------------------------------------------------------------------
# A Tag
# ----------------------------------------------------------------------------------------------

class _TagUpper(mac.UpperLayer):
    """The Tag's upper layer: its Paging ID is its node id, and it sends its node id and the
    value its controller last sensed."""

    def __init__(self, bsm):
        self.bsm = bsm

    def paging_id_matched(self, pid):
        return list(pid) == paging_id(self.bsm.node.id)

    def deliver(self, sdu: bytes):
        self.bsm.heard_sdu(sdu)

    def next_sdu(self) -> bytes:
        return make_sdu(self.bsm.node.id, self.bsm.sensed_value())


class IqTagSession:
    """One Tag: it receives the Reader's waveform and answers with its reflection."""

    def __init__(self, scenario: Any, node_id: int, block_bytes: int, output_dir: str):
        self.control = check_block_bytes(block_bytes)
        self.scenario = scenario
        self.cfg: LinkConfig = scenario.phy
        self.host = TagHost(scenario, node_id)
        self.node_id = node_id
        self.output_dir = output_dir
        bsm = self.host.bsm
        self.bsm = bsm
        self.node = bsm.node
        rng = np.random.default_rng([_seed(scenario), 1, node_id])
        device = mac.Device(_TagUpper(bsm), rng, self.cfg.modulation)
        sector, _, up_dbm = scenario.iq_links().get(node_id, (-1, 0, -999.0))
        incident_w = bsm.downlink_results.get("best_pw_w", {}).get(node_id, 0.0)
        tag_noise = dbm_to_w(self.node.sensitivity_dbm) / 10 ** (self.cfg.tag_snr_db / 10)
        self.link = rlink.TagLink(device, incident_w, math.sqrt(dbm_to_w(up_dbm)) if up_dbm > -999 else 0.0,
                                  tag_noise, rng, self.cfg.m_chip_symb, self.cfg.timing_error)
        self.index = list(scenario.NODE_IDS).index(node_id)
        self.scale = up_scale(thermal_noise_w(scenario.reader_noise_figure_db))
        self._next_ms = 0

    def reply(self, block: bytes) -> Tuple[bytes, bool]:
        if not self.control:  # block n is millisecond n; the run ends after SIM_TIME_MS
            ms = self._next_ms
            air = self._step(ms, from_counts(np.frombuffer(block, dtype="<i2"), TX_COUNTS))
            self._next_ms += 1
            return to_counts(air, self.scale).astype("<i2").tobytes(), self._next_ms == self.scenario.SIM_TIME_MS
        air_in, control_in = split_block(block)
        kind = int(control_in[0])
        control = np.zeros(CONTROL_INT16, dtype=np.int64)
        air = np.zeros(AIR_SAMPLES, dtype=np.complex128)
        done = False
        if kind == MS:
            ms = int(control_in[1]) | int(control_in[2]) << 15
            if ms != self._next_ms:
                raise RuntimeError(f"node {self.node_id} expected {self._next_ms} ms, the Reader sent {ms} ms")
            air = self._step(ms, from_counts(air_in, TX_COUNTS))
            self._next_ms += 1
            control[self.index] = UP
        elif kind == END:
            control[self.index] = BYE
            done = True
        else:  # idle, or nothing yet
            control[self.index] = HELLO
        return join_block(to_counts(air, self.scale), control), done

    def _step(self, ms: int, carrier: np.ndarray) -> np.ndarray:
        self.node.carrier_fraction = float(np.mean(np.abs(carrier) ** 2))
        self.host.deliver(ms, [])
        self.link.powered = self.bsm.powered
        sent = len(self.link.sent)
        out = self.link.step(carrier)
        for _start, s in self.link.sent[sent:]:
            self.bsm.started_d2r(ms, s)
        self.host.step(ms)
        return out

    def finish(self):
        output.write_tag_files(self.output_dir, self.node_id, self.bsm)


def run(scenario: Any, output_dir: str, block_bytes: int = BLOCK_BYTES) -> Tuple[IqReaderSession, List[IqTagSession]]:
    """Run a scenario on one machine, adding the Tags' blocks as rfsim (or rfsim_hub, for air-only
    blocks) does, and write its files. Returns the sessions."""
    os.makedirs(output_dir, exist_ok=True)
    reader = IqReaderSession(scenario, block_bytes, output_dir)
    tags = [IqTagSession(scenario, n, block_bytes, output_dir) for n in scenario.NODE_IDS]
    done = False
    while not done:
        block = reader.next_block()
        answers = [t.reply(block)[0] for t in tags]
        done = reader.on_block(add_blocks(answers))
    reader.finish()
    for t in tags:
        t.finish()
    return reader, tags
