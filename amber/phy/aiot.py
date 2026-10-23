# amber/phy/aiot.py
"""Amber's R2D and D2R PHY: OAI's C library (openair1/PHY/AIOT, libaiot_phy.so) through ctypes.

The signal processing (TS 38.291: CRC, TBCC, Manchester, R2D chips and OFDM waveform, the
device's R2D receiver, the D2R frame, its small-frequency-shift chips and the Reader's D2R
receiver) is all in C; the C files document each step. This module gives it Python types and
draws the receivers' noise from the run's rng, so a run is reproducible from its seed.

The library is found at AMBER_AIOT_LIB, or else at amber/phy/lib/libaiot_phy.so, which
scripts/build_aiot_lib.sh builds from an OAI checkout. The same library holds the A-IoT MAC
(amber.mac), so Amber needs it to load a scenario too: the scenario's protocol is checked there.
"""

import ctypes as C
import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .params import D2RParams, frame_bits, i_bit_values

DEFAULT_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib", "libaiot_phy.so")

MAX_TB_BITS = 1000
MAX_SHIFTS = 8
MAX_SIC_ROUNDS = 16
_MOD = {"ook": 0, "bpsk": 1}


class _Params(C.Structure):
    _fields_ = [("t_bit_num", C.c_int), ("t_bit_den", C.c_int), ("r_sfs", C.c_int), ("r_block", C.c_int),
                ("r_code", C.c_int), ("l_amble", C.c_int), ("i_bit", C.c_int), ("i_add", C.c_int),
                ("modulation", C.c_int)]


class _Expect(C.Structure):
    _fields_ = [("start", C.c_int64), ("te", C.c_int), ("a_bits", C.c_int), ("params", _Params),
                ("r_sfs", C.c_int * MAX_SHIFTS), ("n_r_sfs", C.c_int), ("tag", C.c_int64)]


class _Result(C.Structure):
    _fields_ = [("bits", C.c_uint8 * MAX_TB_BITS), ("n_bits", C.c_int), ("r_sfs", C.c_int), ("start", C.c_int64),
                ("rssi_w", C.c_double), ("tag", C.c_int64)]


class _Outcome(C.Structure):
    _fields_ = [("expect", _Expect), ("decoded", _Result * (MAX_SHIFTS * MAX_SIC_ROUNDS)), ("n_decoded", C.c_int),
                ("collided", C.c_int)]


class _Frame(C.Structure):
    _fields_ = [("bits", C.c_uint8 * (MAX_TB_BITS + 8)), ("n_bits", C.c_int), ("end_sample", C.c_int64),
                ("m_chip_symb", C.c_int)]


_lib = None


def lib_path() -> str:
    return os.environ.get("AMBER_AIOT_LIB") or DEFAULT_LIB


def load(path: Optional[str] = None):
    """Load libaiot_phy.so once and declare the functions used here."""
    global _lib
    if _lib is not None:
        return _lib
    path = path or lib_path()
    if not os.path.exists(path):
        raise RuntimeError(f"Amber's PHY is OAI's C library, and {path} does not exist. Build it with "
                           "scripts/build_aiot_lib.sh, or set AMBER_AIOT_LIB to a libaiot_phy.so.")
    lib = C.CDLL(path)
    u8p, cfp, dp, ip = C.POINTER(C.c_uint8), C.POINTER(C.c_double), C.POINTER(C.c_double), C.c_int
    lib.aiot_crc.argtypes = [u8p, ip, ip, u8p]
    lib.aiot_crc_length.argtypes = [ip]
    lib.aiot_attach_crc.argtypes = [u8p, ip, u8p]
    lib.aiot_tbcc_encode.argtypes = [u8p, ip, u8p]
    lib.aiot_tbcc_encode.restype = None
    lib.aiot_check_crc.argtypes = [u8p, ip, C.POINTER(C.c_int)]
    lib.aiot_tbcc_decode.argtypes = [dp, ip, u8p]
    lib.aiot_repeat_block.argtypes = [u8p, ip, ip, u8p]
    lib.aiot_combine_repetitions.argtypes = [dp, ip, ip, dp]
    lib.aiot_manchester.argtypes = [u8p, ip, u8p]
    lib.aiot_r2d_chips.argtypes = [u8p, ip, ip, u8p, ip]
    lib.aiot_r2d_n_symbols.argtypes = [ip, ip]
    lib.aiot_r2d_n_samples.argtypes = [ip]
    lib.aiot_r2d_waveform.argtypes = [u8p, ip, ip, ip, cfp, ip]
    lib.aiot_r2d_transmission.argtypes = [u8p, ip, ip, ip, cfp, ip]
    lib.aiot_r2d_rx_new.restype = C.c_void_p
    lib.aiot_r2d_rx_free.argtypes = [C.c_void_p]
    lib.aiot_r2d_rx_set_enabled.argtypes = [C.c_void_p, ip]
    lib.aiot_r2d_rx_receive.argtypes = [C.c_void_p, cfp, ip, C.POINTER(_Frame), ip]
    lib.aiot_d2r_coded_bits.argtypes = [u8p, ip, C.POINTER(_Params), u8p, ip]
    lib.aiot_d2r_amble_layout.argtypes = [ip, C.POINTER(_Params), C.POINTER(C.c_int), ip]
    lib.aiot_d2r_assemble.argtypes = [u8p, ip, C.POINTER(_Params), u8p, ip]
    lib.aiot_d2r_sfs_chips.argtypes = [u8p, ip, C.POINTER(_Params), dp, ip]
    lib.aiot_d2r_waveform.argtypes = [u8p, ip, C.POINTER(_Params), dp, ip]
    lib.aiot_frame_samples.argtypes = [ip, C.POINTER(_Params)]
    lib.aiot_d2r_rx_new.argtypes = [C.c_double, ip]
    lib.aiot_d2r_rx_new.restype = C.c_void_p
    lib.aiot_d2r_rx_free.argtypes = [C.c_void_p]
    lib.aiot_d2r_rx_expect.argtypes = [C.c_void_p, C.POINTER(_Expect)]
    lib.aiot_d2r_rx_receive.argtypes = [C.c_void_p, cfp, ip, C.POINTER(_Outcome), ip]
    _lib = lib
    return lib


def _L():
    return _lib if _lib is not None else load()


def _check(rc: int, what: str) -> int:
    if rc < 0:
        raise ValueError(f"{what} failed in the C PHY (error {rc})")
    return rc


def _bits(bits: Sequence[int]):
    a = np.ascontiguousarray(np.asarray(bits, dtype=np.uint8).reshape(-1))
    if not len(a):
        a = np.zeros(1, dtype=np.uint8)
    return a, a.ctypes.data_as(C.POINTER(C.c_uint8))


def _complex(x: np.ndarray):
    a = np.ascontiguousarray(x, dtype=np.complex128)
    return a, a.ctypes.data_as(C.POINTER(C.c_double))


def _params(p: D2RParams) -> _Params:
    from fractions import Fraction
    t = Fraction(p.t_bit_tau)
    return _Params(t.numerator, t.denominator, p.r_sfs, p.r_block, int(p.r_code), p.l_amble, p.i_bit, int(p.i_add),
                   _MOD[p.modulation])


# ----------------------------------------------------------------------------------------------
# Coding (38.291 clause 8)
# ----------------------------------------------------------------------------------------------

def crc_length(a_bits: int) -> int:
    return _L().aiot_crc_length(a_bits)


def crc(bits: Sequence[int], length: int) -> List[int]:
    """The parity bits of the CRC of that length (6 or 16, §8.1)."""
    a, ap = _bits(bits)
    out = np.zeros(length, dtype=np.uint8)
    _check(_L().aiot_crc(ap, len(bits), length, out.ctypes.data_as(C.POINTER(C.c_uint8))), "CRC")
    return out.tolist()


def attach_crc(bits: Sequence[int]) -> List[int]:
    a, ap = _bits(bits)
    out = np.zeros(len(bits) + crc_length(len(bits)), dtype=np.uint8)
    _check(_L().aiot_attach_crc(ap, len(bits), out.ctypes.data_as(C.POINTER(C.c_uint8))), "CRC attachment")
    return out.tolist()


def tbcc_encode(bits: Sequence[int]) -> List[int]:
    """The rate 1/3 tail-biting convolutional code (§8.2)."""
    a, ap = _bits(bits)
    out = np.zeros(3 * len(bits), dtype=np.uint8)
    _L().aiot_tbcc_encode(ap, len(bits), out.ctypes.data_as(C.POINTER(C.c_uint8)))
    return out.tolist()


def check_crc(word: Sequence[int]) -> Tuple[bool, List[int]]:
    """(the CRC holds, the payload) of a received word of B = A + L bits; B in 31..40 cannot be."""
    a, ap = _bits(word)
    n_a = C.c_int(0)
    rc = _L().aiot_check_crc(ap, len(word), C.byref(n_a))
    if rc < 0:
        raise ValueError(f"B = {len(word)} is not a valid payload and CRC length")
    return bool(rc), [int(b) for b in a[:n_a.value]]


def tbcc_decode(soft: Sequence[float]) -> List[int]:
    """Soft Viterbi decoding of the TBCC with wrap-around: 3K soft values (> 0 for a 0), K bits."""
    x = np.ascontiguousarray(np.asarray(soft, dtype=np.float64).reshape(-1))
    if len(x) % 3:
        raise ValueError("the TBCC gives 3 values per bit")
    out = np.zeros(max(len(x) // 3, 1), dtype=np.uint8)
    n = _check(_L().aiot_tbcc_decode(x.ctypes.data_as(C.POINTER(C.c_double)), len(x),
                                     out.ctypes.data_as(C.POINTER(C.c_uint8))), "TBCC decoding")
    return out[:n].tolist()


def repeat_block(bits: Sequence[int], r_block: int) -> List[int]:
    """The block repeated r_block times (§6.1.2.2)."""
    if r_block < 1:
        raise ValueError("r_block must be >= 1")
    a, ap = _bits(bits)
    out = np.zeros(max(len(bits) * r_block, 1), dtype=np.uint8)
    n = _check(_L().aiot_repeat_block(ap, len(bits), r_block, out.ctypes.data_as(C.POINTER(C.c_uint8))),
               "block repetition")
    return out[:n].tolist()


def combine_repetitions(soft: Sequence[float], r_block: int) -> np.ndarray:
    """The r_block equal segments of a received block, summed."""
    x = np.ascontiguousarray(np.asarray(soft, dtype=np.float64).reshape(-1))
    if r_block < 1 or len(x) % r_block:
        raise ValueError("length must be a multiple of r_block")
    out = np.zeros(max(len(x) // r_block, 1), dtype=np.float64)
    n = _check(_L().aiot_combine_repetitions(x.ctypes.data_as(C.POINTER(C.c_double)), len(x), r_block,
                                             out.ctypes.data_as(C.POINTER(C.c_double))), "repetition combining")
    return out[:n]


# ----------------------------------------------------------------------------------------------
# R2D
# ----------------------------------------------------------------------------------------------

@dataclass
class R2DFrame:
    bits: List[int]            # the transport block, CRC removed
    end_sample: int            # the sample after its last chip, in the receiver's sample count
    m_chip_symb: int


def manchester(bits: Sequence[int]) -> List[int]:
    """The Manchester line code (§8.5): two chips per bit."""
    a, ap = _bits(bits)
    out = np.zeros(2 * len(bits), dtype=np.uint8)
    _check(_L().aiot_manchester(ap, len(bits), out.ctypes.data_as(C.POINTER(C.c_uint8))), "Manchester")
    return out.tolist()


def r2d_chips(tb_bits: Sequence[int], m_chip_symb: int) -> List[int]:
    """The chips of an R2D transmission, SIP to padding (§6.2.2 to §6.2.5)."""
    a, ap = _bits(tb_bits)
    chips = np.zeros(2 * (8 + 4 + 2 * (len(tb_bits) + 16) + 4) + 24, dtype=np.uint8)  # room for M = 24's forced chips
    n = _check(_L().aiot_r2d_chips(ap, len(tb_bits), m_chip_symb, chips.ctypes.data_as(C.POINTER(C.c_uint8)),
                                    len(chips)), "R2D chips")
    return chips[:n].tolist()


def n_symbols(n_chips: int, m_chip_symb: int) -> int:
    """OFDM symbols of a transmission of n_chips chips (§6.2.6)."""
    return _check(_L().aiot_r2d_n_symbols(n_chips, m_chip_symb), "R2D symbols")


def r2d_waveform(chips: Sequence[int], m_chip_symb: int, n_rb: int) -> np.ndarray:
    """The OFDM baseband of a chip sequence (§6.2.6, §6.2.7)."""
    a, ap = _bits(chips)
    n = _L().aiot_r2d_n_samples(n_symbols(len(chips), m_chip_symb))
    out = np.zeros(n, dtype=np.complex128)
    _check(_L().aiot_r2d_waveform(ap, len(chips), m_chip_symb, n_rb, out.ctypes.data_as(C.POINTER(C.c_double)), n),
           "R2D waveform")
    return out


def r2d_transmission(tb_bits: Sequence[int], m_chip_symb: int, n_rb: int) -> np.ndarray:
    """The R2D baseband waveform of a transport block, at the sample rate."""
    a, ap = _bits(tb_bits)
    n = _L().aiot_r2d_n_samples(n_symbols(len(r2d_chips(tb_bits, m_chip_symb)), m_chip_symb))
    out = np.zeros(n, dtype=np.complex128)
    _check(_L().aiot_r2d_transmission(ap, len(tb_bits), m_chip_symb, n_rb, out.ctypes.data_as(C.POINTER(C.c_double)),
                                      n), "R2D transmission")
    return out


class R2DReceiver:
    """A device's R2D receiver (an envelope detector), fed one block of samples at a time; the
    receiver noise is added here, from rng."""

    MAX_FRAMES = 64

    def __init__(self, noise_w: float, rng: np.random.Generator):
        self.noise_w = noise_w
        self.rng = rng
        self.enabled = True
        self._rx = _L().aiot_r2d_rx_new()
        if not self._rx:
            raise MemoryError("aiot_r2d_rx_new")
        self._frames = (_Frame * self.MAX_FRAMES)()

    def __del__(self):
        if getattr(self, "_rx", None) and _lib is not None:
            _lib.aiot_r2d_rx_free(self._rx)
            self._rx = None

    def receive(self, x: np.ndarray) -> List[R2DFrame]:
        noise = self.rng.normal(scale=math.sqrt(self.noise_w / 2), size=(2, len(x)))
        y, yp = _complex(x + noise[0] + 1j * noise[1])
        _L().aiot_r2d_rx_set_enabled(self._rx, int(bool(self.enabled)))
        n = _check(_L().aiot_r2d_rx_receive(self._rx, yp, len(y), self._frames, self.MAX_FRAMES), "R2D receiver")
        return [R2DFrame([int(b) for b in f.bits[:f.n_bits]], int(f.end_sample), int(f.m_chip_symb))
                for f in self._frames[:n]]


# ----------------------------------------------------------------------------------------------
# D2R
# ----------------------------------------------------------------------------------------------

def coded_bits(tb_bits: Sequence[int], p: D2RParams) -> List[int]:
    """E: CRC, block repetition and the optional TBCC (§6.1.2)."""
    a, ap = _bits(tb_bits)
    cp = _params(p)
    out = np.zeros(3 * (len(tb_bits) + 16) * p.r_block, dtype=np.uint8)
    n = _check(_L().aiot_d2r_coded_bits(ap, len(tb_bits), C.byref(cp), out.ctypes.data_as(C.POINTER(C.c_uint8)),
                                         len(out)), "D2R coding")
    return out[:n].tolist()


def amble_layout(e_bits: int, p: D2RParams) -> np.ndarray:
    """For each bit on air, the index into the preamble/midamble sequence, or -1 for a data bit
    (§6.1.3)."""
    cp = _layout_params(p)
    n = frame_bits_from_e(e_bits, p)
    out = np.zeros(n, dtype=np.int32)
    _check(_L().aiot_d2r_amble_layout(e_bits, C.byref(cp), out.ctypes.data_as(C.POINTER(C.c_int)), n),
           "D2R amble layout")
    return out.astype(np.int64)


def _layout_params(p) -> _Params:
    """C parameters for the amble layout, which uses only L_amble, I_bit and I_add."""
    return _Params(1, 1, 1, 1, 0, p.l_amble, p.i_bit, int(bool(p.i_add)), _MOD["bpsk"])


def frame_bits_from_e(e_bits: int, p) -> int:
    """V for E coded bits: the ambles added (§6.1.3). p needs l_amble, i_bit and i_add."""
    n_mid = -(-e_bits // p.i_bit) if p.i_add else e_bits // p.i_bit
    return e_bits + (1 + n_mid) * p.l_amble


def assemble(tb_bits: Sequence[int], p: D2RParams) -> List[int]:
    """V: the coded bits with the preamble and midambles in place (§6.1.2, §6.1.3)."""
    a, ap = _bits(tb_bits)
    cp = _params(p)
    n = frame_bits(len(tb_bits), p)
    out = np.zeros(n, dtype=np.uint8)
    _check(_L().aiot_d2r_assemble(ap, len(tb_bits), C.byref(cp), out.ctypes.data_as(C.POINTER(C.c_uint8)), n),
           "D2R assembly")
    return out.tolist()


def sfs_chips(v: Sequence[int], p) -> np.ndarray:
    """The chips of the bits v (§6.1.4, §8.4): 2 R_SFS per bit; 0/1 for OOK, -1/+1 for BPSK.
    p needs r_sfs and modulation."""
    a, ap = _bits(v)
    cp = _Params(2, 1, p.r_sfs, 1, 0, 7, i_bit_values(2)[0], 0, _MOD[p.modulation])  # chips depend on R_SFS only
    out = np.zeros(2 * p.r_sfs * len(v), dtype=np.float64)
    _check(_L().aiot_d2r_sfs_chips(ap, len(v), C.byref(cp), out.ctypes.data_as(C.POINTER(C.c_double)), len(out)),
           "D2R chips")
    return out


def d2r_waveform(tb_bits: Sequence[int], p: D2RParams) -> np.ndarray:
    """The reflection coefficient over time, one value per sample: 0/1 (OOK) or -1/+1 (BPSK)."""
    a, ap = _bits(tb_bits)
    cp = _params(p)
    n = _check(_L().aiot_frame_samples(len(tb_bits), C.byref(cp)), "D2R frame length")
    out = np.zeros(n, dtype=np.float64)
    _check(_L().aiot_d2r_waveform(ap, len(tb_bits), C.byref(cp), out.ctypes.data_as(C.POINTER(C.c_double)), n),
           "D2R waveform")
    return out


class Backscatter:
    """A device's backscatter modulator, fed the carrier it receives one block at a time."""

    def __init__(self, phase: float):
        self.phase = np.exp(1j * phase)
        self._gamma = np.zeros(0)
        self._amp = 0.0

    @property
    def busy(self) -> bool:
        return len(self._gamma) > 0

    def queue(self, tb_bits: Sequence[int], p: D2RParams, amplitude: float, delay: int = 0):
        """Start a frame `delay` samples into the next block. amplitude is what reaches the Reader,
        in sqrt(watt), for a carrier of 1 and a reflection of 1."""
        if self.busy:
            raise RuntimeError("a D2R transmission started while the last one is still going")
        self._gamma = np.concatenate([np.zeros(delay), d2r_waveform(tb_bits, p)])
        self._amp = amplitude

    def next_block(self, carrier: np.ndarray) -> np.ndarray:
        n = len(carrier)
        g = self._gamma[:n]
        self._gamma = self._gamma[n:]
        out = np.zeros(n, dtype=np.complex128)
        out[:len(g)] = self._amp * self.phase * g * carrier[:len(g)]
        return out


# The Reader's receiver searches one sample past each end of the +-te window (aiot_d2r.c).
SEARCH_GUARD = 1


@dataclass
class Expect:
    """A D2R the Reader scheduled: the window to search and how to read it.

    start/te: the expected first sample and the tolerance either side (38.191 §9.2.1).
    a_bits: the transport block size in bits. r_sfs: the shift factors to search (one frame may
    be found on each); the other parameters come from params.
    """
    start: int
    te: int
    a_bits: int
    params: D2RParams
    r_sfs: Sequence[int] = ()
    tag: object = None

    @property
    def shifts(self) -> Tuple[int, ...]:
        return tuple(self.r_sfs) or (self.params.r_sfs,)

    def params_for(self, r: int) -> D2RParams:
        p = self.params
        return D2RParams(p.t_bit_tau, r, p.r_block, p.r_code, p.l_amble, p.i_bit, p.i_add, p.modulation)

    def length(self, r: int) -> int:
        p = self.params_for(r)
        return frame_bits(self.a_bits, p) * p.bit_samples

    @property
    def last_sample(self) -> int:
        return self.start + self.te + SEARCH_GUARD + max(self.length(r) for r in self.shifts)


@dataclass
class D2RResult:
    bits: List[int]
    r_sfs: int
    start: int
    rssi_w: float
    tag: object = None


@dataclass
class Outcome:
    expect: Expect
    decoded: List[D2RResult] = field(default_factory=list)
    collided: int = 0      # preambles found whose frame did not decode


class D2RReceiver:
    """The Reader's D2R receiver: it keeps the samples it may still need and decodes each
    expected D2R once its window has fully arrived; the receiver noise is added here, from rng.
    Each Expect keeps its own tag; the C side gets a number."""

    MAX_OUT = 64

    def __init__(self, noise_w: float, enable_sic: bool, rng: np.random.Generator):
        self.noise_w = noise_w
        self.enable_sic = enable_sic
        self.rng = rng
        self._rx = _L().aiot_d2r_rx_new(noise_w, int(bool(enable_sic)))
        if not self._rx:
            raise MemoryError("aiot_d2r_rx_new")
        self._expects: Dict[int, Expect] = {}
        self._next = 0
        self._out = (_Outcome * self.MAX_OUT)()

    def __del__(self):
        if getattr(self, "_rx", None) and _lib is not None:
            _lib.aiot_d2r_rx_free(self._rx)
            self._rx = None

    def expect(self, e: Expect):
        shifts = tuple(e.r_sfs)
        if len(shifts) > MAX_SHIFTS:
            raise ValueError(f"at most {MAX_SHIFTS} shifts per Expect")
        ce = _Expect(int(e.start), int(e.te), int(e.a_bits), _params(e.params),
                     (C.c_int * MAX_SHIFTS)(*shifts), len(shifts), self._next)
        _check(_L().aiot_d2r_rx_expect(self._rx, C.byref(ce)), "D2R expect")
        self._expects[self._next] = e
        self._next += 1

    def receive(self, x: np.ndarray) -> List[Outcome]:
        noise = self.rng.normal(scale=math.sqrt(self.noise_w / 2), size=(2, len(x)))
        y, yp = _complex(x + noise[0] + 1j * noise[1])
        n = _check(_L().aiot_d2r_rx_receive(self._rx, yp, len(y), self._out, self.MAX_OUT), "D2R receiver")
        out = []
        for o in self._out[:n]:
            e = self._expects.pop(int(o.expect.tag))
            res = Outcome(e, collided=int(o.collided))
            for r in o.decoded[:o.n_decoded]:
                res.decoded.append(D2RResult([int(b) for b in r.bits[:r.n_bits]], int(r.r_sfs), int(r.start),
                                             float(r.rssi_w), e.tag))
            out.append(res)
        return out
