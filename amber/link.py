"""The Rel-19 A-IoT link: 38.391 messages on real waveforms, block by block, at 5.76 Msps.

ReaderLink sends the R2D messages of a Reader (amber.mac.Reader, OAI's C MAC running a protocol)
on the carrier, and opens a D2R receive window for every answer the message can cause, where the
Reader says (T_R->D after the R2D's last chip, searched over +-Te: 38.291 §7.1.2, 38.191 §9.2.1).
TagLink decodes the R2D from the carrier it receives, runs the device MAC (amber.mac.Device) and
reflects its answer at T_R->D after the R2D it answers.

Who decides what to send is the Reader's protocol, in the MAC library; this module only moves
waveforms. The sample clock is the Reader's: block k covers samples [k * B, (k + 1) * B).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from . import mac
from .mac import Answer, D2RSend, Device, Heard, Reader
from .phy import aiot, params

BLOCK = params.SAMPLES_PER_MS           # one millisecond, 5760 samples


# ----------------------------------------------------------------------------------------------
# Reader
# ----------------------------------------------------------------------------------------------

# The Reader's own carrier leaking into its receiver (38.194 Table 7.2.2-1 note: -38 dBm, no phase
# noise cancellation) and the phase noise it carries. The phase noise is a flat floor rising as
# 1/f^2 below the corner; these values put the four 38.194 Annex B.1 FRCs about 1 dB under their
# REFSENS (Table 7.2.2-1) with the default noise figure (OAI's aiot_sim d2r / check).
CW_LEAK_DBM = -38.0
PHASE_NOISE_FLOOR_DBC_HZ = -103.0
PHASE_NOISE_CORNER_HZ = 5300.0


class CarrierLeak:
    """The carrier the Reader sends, as it leaks into its receiver: power_w times the transmitted
    samples, turned by the oscillator's phase noise phi(t).

    phi = a white part (L(f) = floor, variance L * fs per sample) plus a random walk (L(f) =
    floor * (corner / f)^2, increment variance 4 pi^2 corner^2 floor / fs per sample). The receiver
    sees it only where it lands at a D2R's frequency: the bit patterns have zero mean, so the leak
    itself drops out, and its phase noise is what is left."""

    def __init__(self, power_w: float, floor_dbc_hz: float, corner_hz: float, rng: np.random.Generator):
        floor = 10 ** (floor_dbc_hz / 10)
        self.amp = np.sqrt(power_w)
        self.sigma_white = np.sqrt(floor * params.SAMPLE_RATE_HZ)
        self.sigma_step = np.sqrt(4 * np.pi ** 2 * corner_hz ** 2 * floor / params.SAMPLE_RATE_HZ)
        self.rng = rng
        self.walk = rng.uniform(0, 2 * np.pi)

    def __call__(self, carrier: np.ndarray) -> np.ndarray:
        n = len(carrier)
        steps = self.sigma_step * self.rng.standard_normal(n)
        walk = self.walk + np.concatenate([[0.0], np.cumsum(steps[:-1])])
        self.walk = walk[-1] + steps[-1]
        phi = walk + self.sigma_white * self.rng.standard_normal(n)
        return self.amp * carrier * np.exp(1j * phi)


class ReaderLink:
    """The Reader: its MAC's R2D messages out, the D2R answers in.

    reader is the Reader's MAC with its protocol; m_chip_symb and n_rb are the R2D's chips per
    OFDM symbol and PRBs; noise_w the Reader's receiver noise per sample; leak, if given, the
    Reader's carrier leaking into its receiver. on_heard, if given, gets what followed each R2D
    (a mac.Heard) once all its answers are in."""

    def __init__(self, reader: Reader, m_chip_symb: int = 6, n_rb: Optional[int] = None,
                 noise_w: float = 1e-12, enable_sic: bool = True, rng: Optional[np.random.Generator] = None,
                 leak: Optional[CarrierLeak] = None, on_heard: Optional[Callable[[Heard], None]] = None):
        self.m = m_chip_symb
        self.n_rb = n_rb or params.N_RB_MIN[m_chip_symb]
        self.rx = aiot.D2RReceiver(noise_w, enable_sic, rng or np.random.default_rng())
        self.reader = reader
        self.leak = leak
        self.on_heard = on_heard
        self._carrier: Optional[np.ndarray] = None   # what the last next_block sent
        self.t = 0                                   # first sample of the next block
        self._tx = np.zeros(0, dtype=np.complex128)  # R2D samples still to send, from _tx_start
        self._tx_start = 0
        self._expects: Dict[int, object] = {}        # the Reader's windows, by its tag
        self.history: List[Heard] = []
        self.done = False
        self._send()

    def _send(self):
        """Send the Reader's next R2D, if one is ready; one no device answers is followed at once
        by the next."""
        while True:
            r2d = self.reader.next_r2d(self.t)
            if r2d is None:
                self.done = self.reader.done
                return
            _, bits, earliest = r2d
            x = aiot.r2d_transmission(bits, self.m, self.n_rb)
            # l0 = 0: an R2D starts on a half-subframe boundary (38.291 §6.2.7)
            start = -(-earliest // params.HALF_SUBFRAME) * params.HALF_SUBFRAME
            end = start + len(x)
            self._queue_tx(start, x)
            expects = self.reader.r2d_sent(start, end, self.t)
            for e in expects:
                shifts = tuple(e.r_sfs[:e.n_r_sfs])
                self.rx.expect(aiot.Expect(int(e.start), int(e.te), int(e.a_bits), mac.d2r_params(e.params),
                                           shifts, tag=int(e.tag)))
                self._expects[int(e.tag)] = e
            self._collect()
            if expects:
                return

    def _collect(self):
        for h in self.reader.pop_heard():
            self.history.append(h)
            if self.on_heard is not None:
                self.on_heard(h)

    def _queue_tx(self, start: int, x: np.ndarray):
        if len(self._tx):
            gap = start - (self._tx_start + len(self._tx))
            self._tx = np.concatenate([self._tx, np.ones(gap), x])
        else:
            self._tx, self._tx_start = x.astype(np.complex128), start

    # -- one block ----------------------------------------------------------------------------
    def next_block(self) -> np.ndarray:
        """The carrier for samples [t, t + BLOCK): continuous wave, keyed by any R2D in flight."""
        out = np.ones(BLOCK, dtype=np.complex128)
        a, b = self._tx_start - self.t, self._tx_start + len(self._tx) - self.t
        lo, hi = max(a, 0), min(b, BLOCK)
        if hi > lo:
            out[lo:hi] = self._tx[lo - a:hi - a]
        if b <= BLOCK:
            self._tx = np.zeros(0, dtype=np.complex128)
        self._carrier = out
        return out

    def receive(self, x: np.ndarray):
        """The sum of the Tags' reflections over the same block (without the Reader's noise,
        which the receiver adds, or its carrier leak, which is added here)."""
        if self.leak is not None and self._carrier is not None:
            x = x + self.leak(self._carrier[:len(x)])
        self.t += len(x)
        for o in self.rx.receive(x):
            e = self._expects.pop(o.expect.tag, None)
            if e is None:
                continue
            self.reader.d2r_outcome(e, o.decoded, o.collided, self.t)
            self._collect()
            self._send()


# ----------------------------------------------------------------------------------------------
# Tag
# ----------------------------------------------------------------------------------------------

class TagLink:
    """One device: R2D receiver, device MAC, backscatter.

    incident_w is the carrier power reaching the Tag; reflect_amp the amplitude (sqrt W) of its
    reflection at the Reader for a reflection coefficient of 1; noise_w the Tag receiver's noise.
    timing_error is the largest start error as a fraction of Te; each D2R draws one uniformly.
    """

    def __init__(self, mac: Device, incident_w: float, reflect_amp: float, noise_w: float,
                 rng: Optional[np.random.Generator] = None, m_chip_symb: int = 6, timing_error: float = 0.0):
        self.mac = mac
        self.rng = rng or np.random.default_rng()
        self.rx = aiot.R2DReceiver(noise_w, self.rng)
        self.bs = aiot.Backscatter(self.rng.uniform(0, 2 * np.pi))
        self.incident = np.sqrt(incident_w)
        self.amp = reflect_amp
        self.m = m_chip_symb
        self.timing_error = timing_error
        self.t = 0
        self.powered = True
        self.sent: List[Tuple[int, D2RSend]] = []
        self.missed: List[str] = []

    def step(self, carrier: np.ndarray) -> np.ndarray:
        """Take the Reader's carrier for one block (as sent, amplitude 1) and return this Tag's
        reflection as it reaches the Reader over the same block."""
        for f in self.rx.receive(carrier * self.incident):
            if self.powered:
                self._on_frame(f)
        out = self.bs.next_block(carrier)       # a frame in flight runs on, whatever the power
        if not self.powered:
            out[:] = 0                          # but a Tag that is off reflects nothing
        self.t += len(carrier)
        return out

    def _on_frame(self, f: aiot.R2DFrame):
        if f.m_chip_symb != self.m:
            self.m = f.m_chip_symb
        msg = mac.parse_r2d(f.bits)
        if msg is None:
            return
        for s in self.mac.on_r2d(msg):
            off = s.offset(self.m)
            te = params.timing_tolerance(off)
            err = int(round(self.rng.uniform(-1, 1) * self.timing_error * te))
            start = f.end_sample + off + err
            # the frame was found in the block that starts at self.t, before that block's reflection
            # is made, so the backscatter's delay counts from self.t
            delay = start - self.t
            if delay < 0:
                self.missed.append(f"late by {-delay} samples")
                continue
            if self.bs.busy:
                self.missed.append("still sending")
                continue
            self.bs.queue(s.pdu, s.params, self.amp, delay)
            self.sent.append((start, s))


def run_link(reader: ReaderLink, tags: List[TagLink], max_ms: int = 100_000) -> int:
    """Run Reader and Tags block by block until the Reader's protocol ends; returns the blocks run."""
    for k in range(max_ms):
        if reader.done:
            return k
        carrier = reader.next_block()
        reflections = sum((t.step(carrier) for t in tags), np.zeros(BLOCK, dtype=np.complex128))
        reader.receive(reflections)
    raise RuntimeError(f"the Reader's protocol did not end within {max_ms} ms")


def run_script(script, tags: List[TagLink], cfg: Optional["LinkConfig"] = None, m_chip_symb: int = 6,
               n_rb: Optional[int] = None, noise_w: float = 1e-13, enable_sic: bool = True,
               rng: Optional[np.random.Generator] = None, max_ms: int = 100_000) -> List[Heard]:
    """Run a script of Reader messages on the air: script is a generator that yields each R2D
    message (amber.mac builds them) and gets back what it brought (mac.Heard). The Reader runs
    the protocol "manual", which sends only these. Returns every Heard, in order, once the script
    has ended and its last message's answers are in."""
    reader = Reader(cfg or DEFAULT_LINK, "manual", {}, [])
    log: List[Heard] = []
    over = []

    def push(h=None):
        try:
            reader.push(next(script) if h is None else script.send(h))
        except StopIteration:
            over.append(True)

    def heard(h):
        log.append(h)
        push(h)
    push()
    r = ReaderLink(reader, m_chip_symb, n_rb, noise_w, enable_sic, rng, on_heard=heard)
    for _ in range(max_ms):
        if over and reader.idle:
            return log
        carrier = r.next_block()
        r.receive(sum((t.step(carrier) for t in tags), np.zeros(BLOCK, dtype=np.complex128)))
    raise RuntimeError(f"the script did not end within {max_ms} ms")


# ----------------------------------------------------------------------------------------------
# Link settings: the [phy] table of a scenario
# ----------------------------------------------------------------------------------------------
# The link settings a scenario gives in its [phy] table. Every value is a 38.391 or 38.291
# parameter; the Reader's MAC builds every message's D2R Scheduling Info from them, so a protocol
# only decides what 38.391 leaves to the Reader.


@dataclass(frozen=True)
class LinkConfig:
    """[phy] of a scenario. Every key is required: the scenario file alone states what is simulated.

    m_chip_symb       R2D chips per OFDM symbol M: 2, 6, 12 or 24 (38.291 §6.2.4)
    n_rb              R2D PRBs; 0 takes the smallest the M allows (38.291 Table 6.2.7-1)
    t_bit_tau         D2R bit duration as a multiple of tau, "2", "1", "1/2", ... "1/96"
    r_sfs             the R_SFS values a paging offers (its bitmap); at most those the bit
                      duration allows
    x                 time slots of access occasions per paging or Access Trigger, 1 or 2
    r_code            FEC on the D2R
    r_block           block repetition, 1 or 2
    l_amble           D2R preamble and midamble length, 7 or 31
    interval_cp       Interval Bits codepoint 0..3: I_bit = S * {48, 96, 168, 240}
    i_add             additional midamble
    d2r_tbs           D2R transport block for data, in bytes (the SDU gets d2r_tbs - 2)
    modulation        the Tags' D2R modulation, "bpsk" or "ook" (not signalled, 38.291 §7.1.3)
    timing_error      a Tag's largest D2R start error, as a fraction of Te (38.191 §9.2.1)
    tag_snr_db        the R2D SNR a Tag has at its sensitivity_dbm, which sets its noise
    """
    m_chip_symb: int
    n_rb: int
    t_bit_tau: str
    r_sfs: Tuple[int, ...]
    x: int
    r_code: bool
    r_block: int
    l_amble: int
    interval_cp: int
    i_add: bool
    d2r_tbs: int
    modulation: str
    timing_error: float
    tag_snr_db: float

    def __post_init__(self):
        object.__setattr__(self, "r_sfs", tuple(sorted(self.r_sfs)))
        if self.m_chip_symb not in params.M_CHIP_SYMB:
            raise ValueError(f"m_chip_symb must be one of {params.M_CHIP_SYMB}, got {self.m_chip_symb}")
        if self.n_rb and self.n_rb < params.N_RB_MIN[self.m_chip_symb]:
            raise ValueError(f"n_rb {self.n_rb} is below the {params.N_RB_MIN[self.m_chip_symb]} PRBs "
                             f"M = {self.m_chip_symb} needs")
        if not 0 <= self.interval_cp <= 3:
            raise ValueError(f"interval_cp must be 0..3, got {self.interval_cp}")
        if self.modulation not in ("bpsk", "ook"):
            raise ValueError(f"modulation must be 'bpsk' or 'ook', got {self.modulation!r}")
        if not 0 <= self.timing_error <= 1:
            raise ValueError(f"timing_error must be within [0, 1] (a fraction of Te), got {self.timing_error}")
        if not 1 <= self.d2r_tbs <= mac.MAX_TBS:
            raise ValueError(f"d2r_tbs must be 1..{mac.MAX_TBS} bytes, got {self.d2r_tbs}")
        if mac.link_config_check(self) < 0:      # the rest, against 38.391 Table 6.2.1.6-1
            raise ValueError(f"t_bit_tau {self.t_bit_tau}, r_sfs {list(self.r_sfs)}, x {self.x}, r_block {self.r_block}, "
                             f"l_amble {self.l_amble}, interval_cp {self.interval_cp} do not make a valid 38.391 "
                             f"D2R Scheduling Info (Table 6.2.1.6-1)")

    @property
    def bit(self) -> Fraction:
        return Fraction(self.t_bit_tau)

    @property
    def rb(self) -> int:
        return self.n_rb or params.N_RB_MIN[self.m_chip_symb]

    @property
    def max_entries(self) -> int:
        """Devices one Random ID Response can answer."""
        return min(mac.MAX_ID_ENTRIES, len(params.allowed_r_sfs(self.bit)))


# Settings for a Reader whose messages are all scripted (run_script): its protocol builds none.
DEFAULT_LINK = LinkConfig(m_chip_symb=6, n_rb=0, t_bit_tau="1/8", r_sfs=(1, 2, 4, 8), x=1, r_code=True, r_block=1,
                          l_amble=31, interval_cp=0, i_add=False, d2r_tbs=5, modulation="bpsk", timing_error=0.5,
                          tag_snr_db=10.0)
