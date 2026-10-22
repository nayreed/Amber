# amber/phy.py
"""The physical layer of IQ mode: real waveforms for the Reader and the Tags.

In IQ mode rfsim carries IQ samples, not messages (see amber.iq). This module turns bits into
samples and back, following the 3GPP Rel-19 Ambient IoT design for a monostatic Reader (the
Reader itself sends the carrier) and backscatter Tags:

R2D (Reader to Tags)
    The Reader sends a carrier (CW) whenever it is transmitting or listening, and nothing when
    it is idle. A command is keyed onto the carrier with on-off keying (OOK) and Manchester
    coding: a 1 is "on, off", a 0 is "off, on". A frame starts with the delimiter
    "on, off, off, off, on, on, on" (three off chips in a row never happen in Manchester data),
    then an 8-bit length, the command bits and a CRC-16. A Tag decodes it with an envelope
    detector: it compares the energy of the two chips of each bit.

D2R (Tags to Reader)
    A Tag answers by switching its reflection: it multiplies the carrier it receives by
    +1 or -1 (BPSK backscatter), on a square-wave subcarrier, with Manchester coding. A frame is
    a 12-chip preamble, then the 16-bit node id, the 32-bit payload and a CRC-16. A Tag reflects
    only the carrier it receives: without a carrier there is nothing to reflect.
    The Reader removes each subcarrier, finds preambles by correlation at the start of each
    millisecond, decodes the strongest frame, checks its CRC and, with SIC, subtracts it and
    tries again. A preamble that is there but whose frame fails its CRC is a collision.

Signals are complex amplitudes in sqrt(watt); rfsim carries them as 16-bit integers (see
to_counts and from_counts). The sample rate is 1.92 Msps, so one millisecond is 1920 samples.
"""

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

SAMPLE_RATE_HZ = 1_920_000
AIR_SAMPLES = SAMPLE_RATE_HZ // 1000  # samples per millisecond

# rfsim carries 16-bit samples. The Reader sends its carrier at TX_COUNTS, which stands for the
# carrier at whatever power it has; a Tag scales it by the power it receives.
TX_COUNTS = 8192
# What reaches the Reader is written as its 16-bit receiver (ADC) would sample it. The gain puts
# the Reader's thermal noise at ADC_NOISE_COUNTS rms on I and on Q (see up_scale), as a real
# receiver is set up, so weak Tags are limited by the noise and not by rounding. The 16 bits then
# reach about 90 dB above the noise; a Tag stronger than that is clipped, and so is the sum of the
# Tags (rfsim's sum saturates, see iq.add_blocks), like a saturated front end.
ADC_NOISE_COUNTS = 1.0

K_BOLTZMANN = 1.380649e-23
T_REF_K = 290.0

R2D_DELIMITER = (1, 0, 0, 0, 1, 1, 1)
D2R_PREAMBLE = (1, 1, 1, 0, 1, 0, 0, 1, 0, 0, 0, 1)  # a 12-chip pattern with low sidelobes
D2R_ID_BITS = 16
D2R_PAYLOAD_BITS = 32
CRC_BITS = 16

# A Tag's envelope detector: its noise is set so that a command received at the Tag's
# sensitivity has this SNR, about what OOK with Manchester needs to work.
TAG_SNR_AT_SENSITIVITY_DB = 10.0
# The Reader declares a preamble when its correlation is this far above the noise.
DETECT_THRESHOLD_DB = 13.0
MAX_SIC_ROUNDS = 16


def dbm_to_w(dbm: float) -> float:
    return 10.0 ** ((dbm - 30.0) / 10.0)


def w_to_dbm(w: float) -> float:
    return 10.0 * math.log10(w) + 30.0 if w > 0 else -999.0


def thermal_noise_w(noise_figure_db: float) -> float:
    """Thermal noise over the 1.92 MHz the samples cover."""
    return K_BOLTZMANN * T_REF_K * SAMPLE_RATE_HZ * 10.0 ** (noise_figure_db / 10.0)


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


# ----------------------------------------------------------------------------------------------
# Bits and CRC
# ----------------------------------------------------------------------------------------------

def crc16(bits: Sequence[int]) -> List[int]:
    """CRC-16 with the NR polynomial x^16 + x^12 + x^5 + 1 (CCITT), initial value 0xFFFF."""
    reg = 0xFFFF
    for b in bits:
        top = ((reg >> 15) & 1) ^ (b & 1)
        reg = (reg << 1) & 0xFFFF
        if top:
            reg ^= 0x1021
    return int_to_bits(reg, CRC_BITS)


def int_to_bits(value: int, width: int) -> List[int]:
    if value < 0 or value >= 1 << width:
        raise ValueError(f"{value} does not fit in {width} bits")
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


def bits_to_int(bits: Sequence[int]) -> int:
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def manchester(bits: Sequence[int]) -> List[int]:
    out = []
    for b in bits:
        out += (1, 0) if b else (0, 1)
    return out


# ----------------------------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PhyConfig:
    """The link a scenario runs on, from its [phy] table, which every scenario has and which states every value;
    nothing here has a default, so the scenario file is the only source.

    r2d_kbps        Reader-to-Tag bit rate (OOK, Manchester: two chips per bit)
    d2r_kbps        Tag-to-Reader bit rate (BPSK backscatter, Manchester)
    subcarrier_khz  the subcarrier step: a Tag with subcarrier_shift s reflects on (s + 1) times it
    """
    r2d_kbps: int
    d2r_kbps: int
    subcarrier_khz: int

    def __post_init__(self):
        for name, kbps in (("r2d_kbps", self.r2d_kbps), ("d2r_kbps", self.d2r_kbps)):
            chips = 2 * kbps
            if kbps < 1 or AIR_SAMPLES % chips:
                raise ValueError(f"phy.{name} = {kbps}: twice it must divide {AIR_SAMPLES} (chips per millisecond)")
        if self.subcarrier_khz < 1:
            raise ValueError(f"phy.subcarrier_khz must be positive, got {self.subcarrier_khz}")

    @property
    def r2d_samples_per_chip(self) -> int:
        return AIR_SAMPLES // (2 * self.r2d_kbps)

    @property
    def d2r_samples_per_chip(self) -> int:
        return AIR_SAMPLES // (2 * self.d2r_kbps)

    @property
    def d2r_chips_per_ms(self) -> int:
        return 2 * self.d2r_kbps

    def subcarrier_period(self, shift: int) -> int:
        """Samples per subcarrier period for subcarrier_shift; it must fit a whole number of times
        in a chip, so the subcarrier is the same in every chip."""
        f_hz = (shift + 1) * self.subcarrier_khz * 1000
        period = SAMPLE_RATE_HZ / f_hz
        spc = self.d2r_samples_per_chip
        if shift < 0 or period != int(period) or period < 2 or spc % int(period):
            raise ValueError(f"subcarrier_shift {shift} gives {f_hz / 1000:g} kHz, which is not a whole number of "
                             f"samples per period that divides the {spc} samples of a D2R chip")
        return int(period)

    def square_wave(self, shift: int, start: int, n: int) -> np.ndarray:
        """The subcarrier (+1 or -1) for samples start .. start + n - 1 of the run."""
        period = self.subcarrier_period(shift)
        idx = (np.arange(start, start + n) % period) < period // 2
        return np.where(idx, 1.0, -1.0)

    @property
    def d2r_frame_chips(self) -> int:
        return len(D2R_PREAMBLE) + 2 * (D2R_ID_BITS + D2R_PAYLOAD_BITS + CRC_BITS)


# ----------------------------------------------------------------------------------------------
# R2D: the Reader's transmitter and the Tag's envelope detector
# ----------------------------------------------------------------------------------------------

def r2d_frame(bits: Sequence[int]) -> List[int]:
    """The chips of one command: delimiter, 8-bit length, the bits, CRC-16."""
    if len(bits) > 255:
        raise ValueError(f"a command of {len(bits)} bits does not fit in one frame (255 at most)")
    body = int_to_bits(len(bits), 8) + list(bits)
    return list(R2D_DELIMITER) + manchester(body + crc16(body))


class ReaderTransmitter:
    """Keys queued commands onto the carrier, one millisecond at a time."""

    def __init__(self, cfg: PhyConfig):
        self.cfg = cfg
        self._chips: List[int] = []

    @property
    def queued_chips(self) -> int:
        return len(self._chips)

    def queue(self, bits: Sequence[int]):
        self._chips += r2d_frame(bits)

    def next_ms(self, carrier_on: bool) -> np.ndarray:
        """The carrier for the next millisecond, normalized to 1 when on. Queued chips go out
        first, keying the carrier; after them the carrier stays on if carrier_on."""
        spc = self.cfg.r2d_samples_per_chip
        n_chips = AIR_SAMPLES // spc
        chips = self._chips[:n_chips]
        del self._chips[:n_chips]
        fill = 1 if carrier_on else 0
        chips = chips + [fill] * (n_chips - len(chips))
        return np.repeat(np.asarray(chips, dtype=np.float64), spc).astype(np.complex128)


class EnvelopeDetector:
    """A Tag's R2D receiver: an envelope detector, then a chip slicer and a frame decoder.

    Each millisecond it gets the carrier as it reaches the Tag (in sqrt(watt)), adds the
    detector's noise, measures the energy of each chip and decodes the frames whose CRC holds.
    """

    def __init__(self, cfg: PhyConfig, sensitivity_dbm: float, rng: np.random.Generator):
        self.cfg = cfg
        self.rng = rng
        self.sensitivity_w = dbm_to_w(sensitivity_dbm)
        self.noise_w = self.sensitivity_w / 10.0 ** (TAG_SNR_AT_SENSITIVITY_DB / 10.0)
        self._peak = 0.0
        self._state = "search"  # search: looking for a delimiter; data: reading a frame
        self._hist: List[bool] = []  # the last five chips, on or off, while searching
        self._skip = 0  # chips of the delimiter still to pass once it is recognized
        self._chips: List[float] = []  # chip energies of the frame being read
        self._need: Optional[int] = None  # chips the frame has, once its length is known

    def receive(self, x: np.ndarray) -> List[List[int]]:
        """Process one millisecond of received signal; returns the bits of every command decoded."""
        spc = self.cfg.r2d_samples_per_chip
        noise = self.rng.normal(scale=math.sqrt(self.noise_w / 2), size=(2, len(x)))
        y = x + noise[0] + 1j * noise[1]
        energy = (np.abs(y) ** 2).reshape(-1, spc).mean(axis=1)
        out = []
        for e in energy:
            bits = self._chip(float(e))
            if bits is not None:
                out.append(bits)
        return out

    def _chip(self, e: float) -> Optional[List[int]]:
        # The slicing level follows the carrier: half the recent peak, never below half the
        # sensitivity (a real detector's comparator has a floor).
        self._peak = max(e, self._peak * 0.98)
        if self._state == "data":
            if self._skip:
                self._skip -= 1
                return None
            self._chips.append(e)
            return self._frame_progress()
        on = e > max(0.5 * self._peak, 0.5 * self.sensitivity_w)
        self._hist = (self._hist + [on])[-5:]
        if self._hist == [True, False, False, False, True]:
            # on, three off, on: the delimiter; its last two "on" chips follow
            self._state, self._skip, self._chips, self._need, self._hist = "data", 2, [], None, []
        return None

    def _frame_progress(self) -> Optional[List[int]]:
        chips = self._chips
        if self._need is None and len(chips) == 16:
            length = bits_to_int(self._bits(chips))
            self._need = 2 * (8 + length + CRC_BITS)
        if self._need is not None and len(chips) == self._need:
            self._state = "search"
            body = self._bits(chips[:-2 * CRC_BITS])
            crc = self._bits(chips[-2 * CRC_BITS:])
            if crc16(body) == crc:
                return body[8:]
        return None

    @staticmethod
    def _bits(chips: Sequence[float]) -> List[int]:
        return [1 if chips[i] > chips[i + 1] else 0 for i in range(0, len(chips), 2)]


# ----------------------------------------------------------------------------------------------
# D2R: the Tag's reflection and the Reader's receiver
# ----------------------------------------------------------------------------------------------

def d2r_bits(node_id: int, payload: int) -> List[int]:
    if not isinstance(payload, int) or isinstance(payload, bool):
        raise ValueError(f"IQ mode carries integer payloads only, node {node_id} sent {payload!r}")
    if not -(1 << 31) <= payload < 1 << 31:
        raise ValueError(f"node {node_id}'s payload {payload} does not fit in 32 bits")
    body = int_to_bits(node_id, D2R_ID_BITS) + int_to_bits(payload & 0xFFFFFFFF, D2R_PAYLOAD_BITS)
    return body + crc16(body)


def d2r_scrambler(node_id: int) -> np.ndarray:
    """The bits a Tag XORs its frame with (all but the preamble), seeded by its own ID, as NR
    scrambles each device's data with its own identity. Without it, frames of Tags that answer
    together carry mostly the same bits (small IDs and values are mostly zeros), and the Reader
    cannot tell them apart on those bits."""
    rng = np.random.Generator(np.random.PCG64([0x5C4A, node_id]))
    return rng.integers(0, 2, D2R_ID_BITS + D2R_PAYLOAD_BITS + CRC_BITS, dtype=np.int64)


def d2r_chips(node_id: int, payload: int) -> List[int]:
    bits = np.asarray(d2r_bits(node_id, payload)) ^ d2r_scrambler(node_id)
    return list(D2R_PREAMBLE) + manchester([int(b) for b in bits])


class TagReflector:
    """A Tag's backscatter modulator: its reflection, one millisecond at a time."""

    def __init__(self, cfg: PhyConfig, subcarrier_shift: int, phase: float):
        self.cfg = cfg
        self.shift = subcarrier_shift
        cfg.subcarrier_period(subcarrier_shift)  # check it now
        self.phase = np.exp(1j * phase)
        self._chips: List[int] = []
        self._amplitude = 0.0

    @property
    def busy(self) -> bool:
        return bool(self._chips)

    def queue(self, node_id: int, payload: int, power_dbm: float):
        """Start a frame; power_dbm is what reaches the Reader when the carrier is on."""
        if self._chips:
            raise RuntimeError(f"node {node_id} started a transmission while still sending the last one")
        self._chips = d2r_chips(node_id, payload)
        self._amplitude = math.sqrt(dbm_to_w(power_dbm))

    def next_ms(self, carrier: np.ndarray, start_sample: int) -> np.ndarray:
        """The reflection at the Reader for one millisecond, given the normalized carrier the
        Tag receives in it; zero when the Tag is not transmitting."""
        spc = self.cfg.d2r_samples_per_chip
        n_chips = AIR_SAMPLES // spc
        if not self._chips:
            return np.zeros(AIR_SAMPLES, dtype=np.complex128)
        chips = self._chips[:n_chips]
        del self._chips[:n_chips]
        gamma = np.zeros(AIR_SAMPLES)
        gamma[:len(chips) * spc] = np.repeat(np.where(np.asarray(chips) > 0, 1.0, -1.0), spc)
        gamma *= self.cfg.square_wave(self.shift, start_sample, AIR_SAMPLES)
        return self._amplitude * self.phase * gamma * carrier


@dataclass
class Decoded:
    node_id: int
    payload: int
    rssi_dbm: float
    subcarrier_shift: int


class ReaderReceiver:
    """The Reader's D2R receiver for the subcarriers in use.

    Tags start their frames at the start of a millisecond, so the Reader looks for preambles
    only there, like a reader that knows its slots. A frame is decoded once all its chips have
    arrived.
    """

    def __init__(self, cfg: PhyConfig, shifts: Sequence[int], noise_w: float, enable_sic: bool,
                 rng: np.random.Generator, node_ids: Sequence[int]):
        self.cfg = cfg
        # The Tags it may hear: it tries each one's scrambler and keeps the frame whose ID and CRC hold
        self._ids = np.asarray(sorted(set(node_ids)), dtype=np.int64)
        self._scramblers = np.stack([d2r_scrambler(int(n)) for n in self._ids]) if len(self._ids) else \
            np.zeros((0, D2R_ID_BITS + D2R_PAYLOAD_BITS + CRC_BITS), dtype=np.int64)
        self._id_weights = 1 << np.arange(D2R_ID_BITS - 1, -1, -1, dtype=np.int64)
        self.shifts = sorted(set(shifts))
        for s in self.shifts:
            cfg.subcarrier_period(s)
        self.noise_w = noise_w
        self.enable_sic = enable_sic
        self.rng = rng
        self.spc = cfg.d2r_samples_per_chip
        self.cpm = cfg.d2r_chips_per_ms
        self.frame_chips = cfg.d2r_frame_chips
        self.frame_ms = -(-self.frame_chips // self.cpm)
        self._chip_noise = self.noise_w * self.spc  # noise power of one integrated chip
        self._z: Dict[int, np.ndarray] = {s: np.zeros(0, dtype=np.complex128) for s in self.shifts}
        self._first_ms = 0  # the millisecond _z starts at
        self._pre = np.where(np.asarray(D2R_PREAMBLE) > 0, 1.0, -1.0)

    def receive(self, ms: int, x: np.ndarray) -> Tuple[List[Decoded], int]:
        """Process millisecond ms of the summed reflections (in sqrt(watt), without noise yet);
        returns the frames decoded and the number of collisions, both of frames that ended in it."""
        noise = self.rng.normal(scale=math.sqrt(self.noise_w / 2), size=(2, len(x)))
        y = x + noise[0] + 1j * noise[1]
        start = ms * AIR_SAMPLES
        for s in self.shifts:
            sq = self.cfg.square_wave(s, start, AIR_SAMPLES)
            z = (y * sq).reshape(-1, self.spc).sum(axis=1)
            self._z[s] = np.concatenate([self._z[s], z])
        decoded, collided = [], 0
        # Frames that started at ms - frame_ms + 1 end in this millisecond.
        begin = ms - self.frame_ms + 1
        if begin >= self._first_ms:
            for s in self.shifts:
                d, c = self._decode_at(s, (begin - self._first_ms) * self.cpm)
                decoded += d
                collided += c
        # Keep only what a later frame can still need.
        keep_from = ms - self.frame_ms + 2
        if keep_from > self._first_ms:
            drop = (keep_from - self._first_ms) * self.cpm
            for s in self.shifts:
                self._z[s] = self._z[s][drop:]
            self._first_ms = keep_from
        return decoded, collided

    def _detect(self, z: np.ndarray) -> Tuple[bool, complex]:
        """Is a preamble at the start of z? It must stand out from the noise, and look like a
        preamble rather than the middle of a stronger frame. Also returns its amplitude."""
        n = len(self._pre)
        c = np.dot(self._pre, z[:n])
        above_noise = abs(c) ** 2 / (n * self._chip_noise) > 10 ** (DETECT_THRESHOLD_DB / 10)
        shape = abs(c) ** 2 / (n * max(float(np.sum(np.abs(z[:n]) ** 2)), 1e-300))
        return above_noise and shape > 0.5, c / (n * self.spc)

    def _descramble(self, bits: np.ndarray) -> Optional[Tuple[int, int]]:
        """(node id, payload) of the first known Tag whose scrambler gives back its own ID and a CRC
        that holds, or None."""
        plain = bits[None, :] ^ self._scramblers
        ids = plain[:, :D2R_ID_BITS] @ self._id_weights
        for row in np.flatnonzero(ids == self._ids):
            body = [int(b) for b in plain[row, :-CRC_BITS]]
            if crc16(body) == [int(b) for b in plain[row, -CRC_BITS:]]:
                return int(self._ids[row]), bits_to_int(body[D2R_ID_BITS:])
        return None

    def _decode_at(self, s: int, k: int) -> Tuple[List[Decoded], int]:
        view = self._z[s][k:k + self.frame_chips]  # decoded frames are taken out of _z itself
        z0 = view.copy()
        z = z0
        decoded: List[Decoded] = []
        frames: List[np.ndarray] = []  # the chips (+1/-1) of each decoded frame
        found, a = self._detect(z)
        if not found:
            return decoded, 0
        for _ in range(MAX_SIC_ROUNDS):
            soft = (z[len(self._pre):] * np.conj(a)).real
            bits = np.asarray([1 if soft[i] > soft[i + 1] else 0 for i in range(0, len(soft), 2)], dtype=np.int64)
            frame = self._descramble(bits)
            if frame is None:
                break
            node_id, payload = frame
            if payload >= 1 << 31:
                payload -= 1 << 32
            if any(d.node_id == node_id and d.payload == payload for d in decoded):
                break  # what is left is this frame again, not a new one
            frames.append(np.where(np.asarray(d2r_chips(node_id, payload)) > 0, 1.0, -1.0))
            decoded.append(Decoded(node_id, payload, 0.0, s))
            # Estimate the amplitudes of all decoded frames together from everything received,
            # take them out, and with SIC look again in what is left.
            m = np.stack(frames, axis=1) * self.spc
            amps = np.linalg.lstsq(m, z0, rcond=None)[0]
            z = z0 - m @ amps
            for d, amp in zip(decoded, amps):
                d.rssi_dbm = w_to_dbm(abs(amp) ** 2)
            found, a = self._detect(z)
            if not found or not self.enable_sic:
                break
        view[:] = z
        # What is left is a preamble the Reader heard but could not decode: a collision. The
        # Reader cannot tell how many frames it holds, so it counts one.
        return decoded, 1 if found else 0
