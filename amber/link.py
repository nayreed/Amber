"""The Rel-19 A-IoT link: 38.391 messages on real waveforms, block by block, at 5.76 Msps.

ReaderLink sends R2D messages on the carrier and opens a D2R receive window for every answer the
message can cause, at T_R->D after the R2D's last chip, searched over +-Te (38.291 §7.1.2,
38.191 §9.2.1). TagLink decodes the R2D from the carrier it receives, runs the device MAC
(amber.mac) and reflects its answer at T_R->D after the R2D it answers.

Who decides what to send is a Reader policy: a generator that yields R2D messages and gets back
what was heard after each one (Heard). A protocol is such a policy; it only chooses 38.391
messages and their fields, so the device side is the same for every protocol.

The sample clock is the Reader's: block k covers samples [k * B, (k + 1) * B).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Generator, List, Optional, Sequence, Tuple

import numpy as np

from .mac import D2RSend, DeviceMAC
from .mac import pdu as mp
from .phy import aiot, params

BLOCK = params.SAMPLES_PER_MS           # one millisecond, 5760 samples


# ----------------------------------------------------------------------------------------------
# What the Reader hears
# ----------------------------------------------------------------------------------------------

@dataclass
class Answer:
    """One D2R the Reader decoded.

    kind is "msg1" (an Access Random ID; message is mp.AccessRandomID) or "data" (message is
    mp.D2RUpperLayerDataTransfer). slot is the Msg1 time slot, entry the Random ID Response
    entry it answers (or None)."""
    kind: str
    message: object
    r_sfs: int
    rssi_w: float
    start: int
    slot: int = 0
    entry: Optional[int] = None
    as_id: Optional[int] = None


@dataclass
class Heard:
    """What followed one R2D message: the answers decoded and the preambles found whose frame did
    not decode (collisions, or a frame lost in noise after its preamble was seen)."""
    sent: object
    answers: List[Answer] = field(default_factory=list)
    collided: int = 0
    r2d_start: int = 0
    r2d_end: int = 0

    @property
    def empty(self) -> bool:
        return not self.answers and not self.collided


Policy = Generator[object, Heard, None]


def d2r_chips(a_bits: int, p: params.D2RParams) -> int:
    """M_chip^D2R: the chips of one D2R transmission (38.291 §7.1.2)."""
    return params.frame_bits(a_bits, p) * 2 * p.r_sfs


def d2r_start_offset(send_msg: str, p: params.D2RParams, m_chip_symb: int, tbs_bytes: int,
                     candidates=(), slot: int = 0) -> int:
    """T_R->D in samples, for what the device sends and the Reader expects alike."""
    a_bits = 8 * tbs_bytes
    return params.t_r_to_d(p, m_chip_symb, send_msg, tbs_bytes, candidates, slot > 0,
                           d2r_chips(a_bits, p))


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
    """The Reader: its policy's R2D messages out, the D2R answers in.

    m_chip_symb and n_rb are the R2D's chips per OFDM symbol and PRBs; noise_w the Reader's
    receiver noise per sample; leak, if given, the Reader's carrier leaking into its receiver. The
    last CBRA paging's Scheduling Info is kept, since an Access Trigger opens the same occasions."""

    def __init__(self, policy: Policy, m_chip_symb: int = 6, n_rb: Optional[int] = None,
                 noise_w: float = 1e-12, enable_sic: bool = True, rng: Optional[np.random.Generator] = None,
                 leak: Optional[CarrierLeak] = None):
        self.m = m_chip_symb
        self.n_rb = n_rb or params.N_RB_MIN[m_chip_symb]
        self.rx = aiot.D2RReceiver(noise_w, enable_sic, rng or np.random.default_rng())
        self.policy = policy
        self.leak = leak
        self._carrier: Optional[np.ndarray] = None   # what the last next_block sent
        self.t = 0                                   # first sample of the next block
        self._tx = np.zeros(0, dtype=np.complex128)  # R2D samples still to send, from _tx_start
        self._tx_start = 0
        self._free_at = 0                            # earliest start of the next R2D
        self._cbra: Optional[mp.SchedulingInfo] = None
        self._open: Optional[Heard] = None
        self._waiting = 0                            # expected D2Rs not yet resolved
        self._last_d2r_end = 0
        self.history: List[Heard] = []
        self.done = False
        self._advance(None)

    # -- the policy --------------------------------------------------------------------------
    def _advance(self, heard: Optional[Heard]):
        try:
            msg = self.policy.send(heard) if heard is not None else next(self.policy)
        except StopIteration:
            self.done = True
            return
        self._send(msg)

    def _send(self, msg):
        bits = msg.pack()
        x = aiot.r2d_transmission(bits, self.m, self.n_rb)
        # l0 = 0: an R2D starts on a half-subframe boundary (38.291 §6.2.7)
        start = -(-max(self._free_at, self.t) // params.HALF_SUBFRAME) * params.HALF_SUBFRAME
        end = start + len(x)
        self._queue_tx(start, x)
        heard = Heard(sent=msg, r2d_start=start, r2d_end=end)
        self._open, self._waiting = heard, 0
        for e in self._expects(msg, end):
            self.rx.expect(e)
            self._waiting += 1
            # the next R2D waits for the latest possible D2R end plus T_D2R_min (38.191 §9.3), the
            # time after its D2R a device need not yet monitor R2D
            self._last_d2r_end = max(self._last_d2r_end, max(
                e.start + e.te + e.length(r) + params.t_d2r_min(e.params_for(r)) for r in e.shifts))
        self._free_at = end
        if self._waiting == 0:
            self._close()

    def _queue_tx(self, start: int, x: np.ndarray):
        if len(self._tx):
            gap = start - (self._tx_start + len(self._tx))
            self._tx = np.concatenate([self._tx, np.ones(gap), x])
        else:
            self._tx, self._tx_start = x.astype(np.complex128), start

    def _expects(self, msg, end: int) -> List[aiot.Expect]:
        out = []
        if isinstance(msg, mp.AIoTPagingCBRA):
            self._cbra = msg.sched
        if isinstance(msg, (mp.AIoTPagingCBRA, mp.AccessTrigger)):
            s = self._cbra
            if s is None:
                return []
            for slot in range(s.x):
                p = mp.to_d2r_params(s, s.r_sfs[0])
                off = d2r_start_offset("msg1", p, self.m, 2, s.r_sfs, slot)
                out.append(aiot.Expect(end + off, params.timing_tolerance(off), 16, p, s.r_sfs,
                                      tag=("msg1", slot, None, None)))
        elif isinstance(msg, mp.RandomIDResponse):
            s = msg.sched
            for i, e in enumerate(msg.entries):
                p = mp.to_d2r_params(s, s.r_sfs[i])
                off = d2r_start_offset("msg2", p, self.m, s.d2r_tbs, s.r_sfs)
                out.append(aiot.Expect(end + off, params.timing_tolerance(off), 8 * s.d2r_tbs, p, (s.r_sfs[i],),
                                      tag=("data", 0, i, mp_as_id(e))))
        elif isinstance(msg, mp.AIoTPagingCFA):
            s = msg.sched
            p = mp.to_d2r_params(s, s.r_sfs[0])
            off = d2r_start_offset("cfa", p, self.m, s.d2r_tbs)
            out.append(aiot.Expect(end + off, params.timing_tolerance(off), 8 * s.d2r_tbs, p, s.r_sfs,
                                  tag=("data", 0, None, None)))
        elif isinstance(msg, mp.R2DUpperLayerDataTransfer):
            s = msg.sched
            p = mp.to_d2r_params(s, s.r_sfs[0])
            off = d2r_start_offset("data", p, self.m, s.d2r_tbs)
            out.append(aiot.Expect(end + off, params.timing_tolerance(off), 8 * s.d2r_tbs, p, s.r_sfs,
                                  tag=("data", 0, None, msg.as_id)))
        return out

    def _close(self):
        heard, self._open = self._open, None
        self.history.append(heard)
        if self._last_d2r_end > heard.r2d_end:
            self._free_at = max(self._free_at, self._last_d2r_end)
        self._advance(heard)

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
            kind, slot, entry, as_id = o.expect.tag
            heard = self._open
            if heard is None:
                continue
            heard.collided += o.collided
            for res in o.decoded:
                m = _parse_d2r(kind, res.bits)
                if m is None:
                    heard.collided += 1
                    continue
                heard.answers.append(Answer(kind, m, res.r_sfs, res.rssi_w, res.start, slot, entry, as_id))
            self._waiting -= 1
            if self._waiting == 0:
                self._close()


def mp_as_id(entry: mp.RandomIDEntry) -> int:
    return entry.assigned_as_id if entry.assigned_as_id is not None else entry.echoed_random_id


def _parse_d2r(kind: str, bits):
    try:
        if kind == "msg1":
            return mp.parse_d2r(bits, mp.D2R_ACCESS_RANDOM_ID)
        return mp.parse_d2r(bits, mp.D2R_UPPER_LAYER_DATA_KIND, d2r_tbs=len(bits) // 8)
    except ValueError:
        return None   # passed the CRC but is not a valid PDU


# ----------------------------------------------------------------------------------------------
# Tag
# ----------------------------------------------------------------------------------------------

class TagLink:
    """One device: R2D receiver, device MAC, backscatter.

    incident_w is the carrier power reaching the Tag; reflect_amp the amplitude (sqrt W) of its
    reflection at the Reader for a reflection coefficient of 1; noise_w the Tag receiver's noise.
    timing_error is the largest start error as a fraction of Te; each D2R draws one uniformly.
    """

    def __init__(self, mac: DeviceMAC, incident_w: float, reflect_amp: float, noise_w: float,
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
        try:
            msg = mp.parse_r2d(f.bits)
        except ValueError:
            return
        for s in self.mac.on_r2d(msg):
            off = d2r_start_offset(s.msg, s.params, self.m, s.tbs_bytes, s.candidate_r_sfs, s.slot)
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
    """Run Reader and Tags block by block until the Reader's policy ends; returns the blocks run."""
    for k in range(max_ms):
        if reader.done:
            return k
        carrier = reader.next_block()
        reflections = sum((t.step(carrier) for t in tags), np.zeros(BLOCK, dtype=np.complex128))
        reader.receive(reflections)
    raise RuntimeError(f"the Reader's policy did not end within {max_ms} ms")


# ----------------------------------------------------------------------------------------------
# Link settings: the [phy] table of a scenario
# ----------------------------------------------------------------------------------------------
#The link settings a scenario gives in its [phy] table, and the D2R Scheduling Info they make.
#
#Every value is a 38.391 or 38.291 parameter. A Reader policy builds its messages' Scheduling Info
#from these (LinkConfig.cbra, .ridr, .cfa, .unicast), so a protocol only decides what 38.391 leaves
#to the Reader: how many access occasions, whom to page, when to trigger, which AS ID to assign.


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
        if not 1 <= self.d2r_tbs <= mp.MAX_TBS:
            raise ValueError(f"d2r_tbs must be 1..{mp.MAX_TBS} bytes, got {self.d2r_tbs}")
        self.cbra()          # checks the rest against 38.391 Table 6.2.1.6-1

    @property
    def bit(self) -> Fraction:
        return Fraction(self.t_bit_tau)

    @property
    def rb(self) -> int:
        return self.n_rb or params.N_RB_MIN[self.m_chip_symb]

    def _common(self):
        return dict(t_bit_tau=self.bit, r_block=self.r_block, r_code=self.r_code,
                    i_bit=params.i_bit_values(self.bit)[self.interval_cp], l_amble=self.l_amble,
                    i_add=self.i_add)

    def cbra(self, r_sfs: Optional[Sequence[int]] = None, x: Optional[int] = None) -> mp.SchedulingInfo:
        """Scheduling Info of a CBRA paging: its m = x * len(r_sfs) occasions per R2D."""
        return mp.SchedulingInfo(mp.SCHED_PAGING_CBRA, r_sfs=tuple(sorted(r_sfs or self.r_sfs)),
                                 x=x or self.x, **self._common())

    def ridr(self, n_entries: int, d2r_tbs: Optional[int] = None) -> mp.SchedulingInfo:
        """Scheduling Info of a Random ID Response for n_entries devices: entry i answers on the
        i-th R_SFS, so the bitmap holds the n_entries smallest values the bit duration allows."""
        allowed = params.allowed_r_sfs(self.bit)
        if not 1 <= n_entries <= len(allowed):
            raise ValueError(f"{n_entries} entries need as many R_SFS values; bit duration {self.bit} tau "
                             f"allows {len(allowed)}")
        return mp.SchedulingInfo(mp.SCHED_RANDOM_ID_RESPONSE, r_sfs=tuple(allowed[:n_entries]),
                                 d2r_tbs=d2r_tbs or self.d2r_tbs, **self._common())

    def cfa(self, r_sfs: Optional[int] = None, d2r_tbs: Optional[int] = None) -> mp.SchedulingInfo:
        return mp.SchedulingInfo(mp.SCHED_PAGING_CFA, r_sfs=(r_sfs or self.r_sfs[0],),
                                 d2r_tbs=d2r_tbs or self.d2r_tbs, **self._common())

    def unicast(self, r_sfs: Optional[int] = None, d2r_tbs: Optional[int] = None) -> mp.SchedulingInfo:
        return mp.SchedulingInfo(mp.SCHED_R2D_DATA, r_sfs=(r_sfs or self.r_sfs[0],),
                                 d2r_tbs=d2r_tbs or self.d2r_tbs, **self._common())

    @property
    def max_entries(self) -> int:
        """Devices one Random ID Response can answer."""
        return min(mp.MAX_ID_ENTRIES, len(params.allowed_r_sfs(self.bit)))
