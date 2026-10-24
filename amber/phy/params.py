# amber/params.py
"""Constants, sequences and parameter tables of the Rel-19 Ambient IoT physical layer.

Sources: 3GPP TS 38.291 V19.3.0 (PHY) and TS 38.391 V19.4.0 (MAC, Table 6.2.1.6-1, which lists the
values each D2R parameter can take). Times are counted in samples at SAMPLE_RATE_HZ, where every
spec duration used here is a whole number of samples (see the log, decisions of 2026-10-22).
"""

from dataclasses import dataclass
from fractions import Fraction
from typing import List, Sequence, Tuple

# ----------------------------------------------------------------------------------------------
# Sampling and the R2D OFDM numerology (38.291 §4.3, §6.2.7)
# ----------------------------------------------------------------------------------------------

SAMPLE_RATE_HZ = 5_760_000
SAMPLES_PER_MS = SAMPLE_RATE_HZ // 1000          # 5760
SCS_HZ = 15_000                                  # Δf, §4.3.3.1
N_FFT = SAMPLE_RATE_HZ // SCS_HZ                 # 384: N_u = 2048κ·T_c at this rate
CP_LONG = 30                                     # 160κ·T_c in samples, for l_RE mod 7 = l0
CP_SHORT = 27                                    # 144κ·T_c in samples, otherwise
HALF_SUBFRAME = 7 * N_FFT + CP_LONG + 6 * CP_SHORT  # 2880 samples = 0.5 ms
SC_PER_RB = 12

# tau = 2e6 / 15000 µs (38.291 §4.2), in samples: 768 at 5.76 Msps.
TAU = Fraction(2_000_000, SCS_HZ) * Fraction(SAMPLE_RATE_HZ, 1_000_000)
assert TAU == 768

# ----------------------------------------------------------------------------------------------
# Fixed bit sequences
# ----------------------------------------------------------------------------------------------

SIP = (1, 1, 0, 0, 1, 0, 0, 0)        # R-TAS start indicator part, §6.2.3.1
CAP = (1, 0, 1, 0)                    # R-TAS clock acquisition part, §6.2.3.2
POSTAMBLE = (1, 1, 1, 1)              # R2D postamble, §6.2.4
SIP_CHIPS_PER_SYMBOL = 4              # §4.3.2
N_SIP = len(SIP)
N_CAP = len(CAP)

M_CHIP_SYMB = (2, 6, 12, 24)          # R2D chips per OFDM symbol, Table 4.3.3.3-1
N_RB_MIN = {2: 1, 6: 1, 12: 2, 24: 3}  # minimum PRBs for each M, Table 4.3.3.3-1
N_RB_R2D = (1, 2, 3, 4)               # R2D channel of 200/400/600/800 kHz, 38.191 Table 5.3.1.2-1


def cp_length(l_re: int, l0: int = 0) -> int:
    """The cyclic prefix of OFDM symbol l_re in samples: long for l_RE mod 7 = l0 (38.291 §6.2.7)."""
    return CP_LONG if l_re % 7 == l0 else CP_SHORT


def symbol_starts(n_sym: int, l0: int = 0) -> List[int]:
    """Sample index where each symbol (its CP) starts, from the start of symbol 0."""
    starts, t = [], 0
    for l in range(n_sym):
        starts.append(t)
        t += cp_length(l, l0) + N_FFT
    starts.append(t)
    return starts


def m_sequence(length: int) -> Tuple[int, ...]:
    """The D2R preamble or midamble, 38.291 §8.3, built from its recursion."""
    if length == 7:
        x, taps = [1, 0, 0], (2, 0)        # x[m+3] = x[m+2] + x[m]
    elif length == 31:
        x, taps = [0, 1, 0, 0, 1], (3, 0)  # x[m+5] = x[m+3] + x[m]
    else:
        raise ValueError(f"D2R amble length is 7 or 31, not {length}")
    n0 = len(x)
    while len(x) < length:
        m = len(x) - n0
        x.append(x[m + taps[0]] ^ x[m + taps[1]])
    return tuple(x)


# ----------------------------------------------------------------------------------------------
# D2R parameters (38.291 §7.1.1, values from 38.391 Table 6.2.1.6-1)
# ----------------------------------------------------------------------------------------------

# Bit Duration codepoints, in units of tau
BIT_DURATIONS = (Fraction(2), Fraction(1), Fraction(1, 2), Fraction(1, 4), Fraction(1, 8),
                 Fraction(1, 16), Fraction(1, 32), Fraction(1, 96))
R_SFS_VALUES = (1, 2, 4, 8, 16, 32, 64, 128)
# Largest R_SFS allowed for each bit duration, and the I_bit scale factor S
_MAX_R_SFS = dict(zip(BIT_DURATIONS, (128, 64, 32, 16, 8, 4, 2, 1)))
_I_BIT_SCALE = dict(zip(BIT_DURATIONS, (1, 2, 4, 8, 16, 32, 64, 192)))
I_BIT_BASE = (48, 96, 168, 240)
D2R_TBS_BYTES = range(1, 126)


def allowed_r_sfs(t_bit_tau: Fraction) -> Tuple[int, ...]:
    """The small frequency shift factors 38.391 allows with this bit duration."""
    return tuple(r for r in R_SFS_VALUES if r <= _MAX_R_SFS[Fraction(t_bit_tau)])


def i_bit_values(t_bit_tau: Fraction) -> Tuple[int, ...]:
    """The midamble intervals 38.391 allows with this bit duration (codepoint times S)."""
    s = _I_BIT_SCALE[Fraction(t_bit_tau)]
    return tuple(s * i for i in I_BIT_BASE)


@dataclass(frozen=True)
class D2RParams:
    """What higher layers give the device for one D2R transmission (38.291 §7.1.1).

    t_bit_tau   T_bit in units of tau (one of BIT_DURATIONS)
    r_sfs       small frequency shift factor
    r_block     block repetition number, 1 or 2
    r_code      True when the TBCC is used
    l_amble     7 (short) or 31 (long)
    i_bit       interval in bits between midambles (already scaled by S)
    i_add       True for the additional midamble at the end
    modulation  "ook" or "bpsk" (§7.1.3: the device's own choice for Msg1 and CFA)
    """
    t_bit_tau: Fraction = Fraction(1, 8)
    r_sfs: int = 4
    r_block: int = 1
    r_code: bool = False
    l_amble: int = 31
    i_bit: int = 768
    i_add: bool = False
    modulation: str = "bpsk"

    def __post_init__(self):
        t = Fraction(self.t_bit_tau)
        if t not in _MAX_R_SFS:
            raise ValueError(f"bit duration {t}·tau is not one of 38.391's {[str(b) for b in BIT_DURATIONS]}")
        if self.r_sfs not in allowed_r_sfs(t):
            raise ValueError(f"R_SFS {self.r_sfs} is not allowed with bit duration {t}·tau; allowed: {allowed_r_sfs(t)}")
        if self.r_block not in (1, 2):
            raise ValueError(f"block repetition is 1 or 2, not {self.r_block}")
        if self.l_amble not in (7, 31):
            raise ValueError(f"amble length is 7 or 31, not {self.l_amble}")
        if self.i_bit not in i_bit_values(t):
            raise ValueError(f"I_bit {self.i_bit} is not allowed with bit duration {t}·tau; allowed: {i_bit_values(t)}")
        if self.modulation not in ("ook", "bpsk"):
            raise ValueError(f"D2R modulation is 'ook' or 'bpsk', not {self.modulation!r}")

    @property
    def bit_samples(self) -> int:
        n = TAU * Fraction(self.t_bit_tau)
        assert n.denominator == 1
        return int(n)

    @property
    def chip_samples(self) -> int:
        """T_chip = T_bit / (2 R_SFS), §7.1.1."""
        n = Fraction(self.bit_samples, 2 * self.r_sfs)
        if n.denominator != 1:
            raise ValueError(f"a D2R chip is {n} samples at {SAMPLE_RATE_HZ} Hz, not a whole number")
        return int(n)

    @property
    def nominal_bw_hz(self) -> float:
        """The device's nominal D2R transmission bandwidth without SFO, 4 / T_bit
        (38.191 Table 5.3.2.3-1 rows; 15 kHz at 2 tau)."""
        return 4 * SAMPLE_RATE_HZ / self.bit_samples

    @property
    def shift_hz(self) -> float:
        """The small frequency shift R_SFS / T_bit."""
        return self.r_sfs * SAMPLE_RATE_HZ / self.bit_samples


def frame_bits(a_bits: int, p: D2RParams) -> int:
    """V, the number of bits on air for a transport block of a_bits bits (§6.1.2, §6.1.3)."""
    b = a_bits + (16 if a_bits > 24 else 6)
    e = b * p.r_block * (3 if p.r_code else 1)
    # floor(E / I_bit) midambles, or ceil(E / I_bit) with the additional one (§6.1.3): the
    # additional midamble closes a last, shorter block and coincides with the last regular one
    # when E is a multiple of I_bit
    n_mid = -(-e // p.i_bit) if p.i_add else e // p.i_bit
    return e + (1 + n_mid) * p.l_amble


def frame_samples(a_bits: int, p: D2RParams) -> int:
    return frame_bits(a_bits, p) * p.bit_samples


# ----------------------------------------------------------------------------------------------
# D2R start time (38.291 §7.1.2, V19.3.0)
# ----------------------------------------------------------------------------------------------

def _t_offset(t_chip_tau: Fraction, m_chip_symb: int) -> Fraction:
    """Table 7.1.2-3, in units of tau."""
    if t_chip_tau >= Fraction(1, 2):
        return Fraction(10)
    if t_chip_tau >= Fraction(1, 16):
        return Fraction(5)
    if t_chip_tau == Fraction(1, 32):
        return Fraction(1)
    return Fraction(1) if m_chip_symb == 2 else Fraction(1, 4)


def t_r_to_d(p: D2RParams, m_chip_symb: int, msg: str, n_tbs_bytes: int = 0,
             candidate_r_sfs: Sequence[int] = (), earlier_occasion: bool = False,
             d2r_chips: int = 0) -> int:
    """T_R->D in samples: from the end of the last R2D chip to the first D2R chip.

    msg is "msg1" (Access Random ID), "msg2" (answers a Random ID Response), "cfa" (answers a
    contention-free paging) or "data" (any other D2R). candidate_r_sfs is the set of potential
    R_SFS for Msg1/Msg2 (defaults to p.r_sfs). For Msg1, earlier_occasion says whether an access
    occasion earlier than the selected one follows the R2D, and d2r_chips is M_chip^D2R of the
    transmission (needed only then).
    """
    t_chip = Fraction(p.t_bit_tau) / (2 * p.r_sfs)
    if msg in ("msg1", "msg2"):
        cands = tuple(candidate_r_sfs) or (p.r_sfs,)
        t_chip_prime = max(Fraction(p.t_bit_tau) / (2 * r) for r in cands)
    elif msg in ("cfa", "data"):
        t_chip_prime = t_chip
    else:
        raise ValueError(f"unknown D2R message kind {msg!r}")
    t_off = _t_offset(t_chip_prime, m_chip_symb)
    if msg == "msg1":
        t = Fraction(5, 4) * (t_off + d2r_chips * t_chip) if earlier_occasion else t_off
    elif msg in ("msg2", "cfa") and n_tbs_bytes <= 32:
        # Table 7.1.2-1 (only with channel coding)
        t = t_off + ((Fraction(1) if t_chip_prime <= Fraction(1, 32) else Fraction(0)) if p.r_code else 0)
    else:
        # Table 7.1.2-2 (only with channel coding)
        if p.r_code:
            d = 2 if n_tbs_bytes <= 32 else 4 if n_tbs_bytes <= 64 else 8
            t = t_off + d
        else:
            t = t_off
    n = t * TAU
    if n.denominator != 1:
        raise ValueError(f"T_R->D = {t}·tau is not a whole number of samples")
    return int(n)


def timing_tolerance(t_sfo_samples: int) -> int:
    """Te = 10% of T_SFO (38.191 §9.2.1), rounded up, in samples."""
    return -(-t_sfo_samples // 10)


def t_d2r_min(p: D2RParams) -> int:
    """T_D2R_min = max(3 T_chip, 10 µs) (38.191 §9.3), in samples."""
    return max(3 * p.chip_samples, SAMPLE_RATE_HZ // 100_000)
