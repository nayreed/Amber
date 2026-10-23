# amber/mac_pdu.py
"""Packer and parser for the Rel-19 Ambient IoT MAC messages, 3GPP TS 38.391 V19.0.0 clause 6.

Bit conventions (6.1.1, PDF p.13): a PDU is a bit string, byte aligned (a multiple of 8 bits) except
the Access Trigger message, which is only its 3-bit R2D Message Type. Fields are read left to right
and top to bottom of the spec figures, most significant bit first. Everything here is a list of 0/1
ints, MSB first. Fill Bits (R2D) and MAC Padding (D2R) sit at the end of the PDU.

Message types (Table 6.1-1 and 6.1-2, p.14): R2D 3 bits, 001 Paging, 010 Access Trigger, 011 Random
ID Response, 100 R2D Upper Layer Data Transfer, 101 NACK Feedback, 000/110/111 reserved. D2R: the
Access Random ID (Msg1) has no type field and is the bare 16-bit Random ID; the D2R Upper Layer Data
Transfer has a 2-bit type 00 (01, 10, 11 reserved).

R2D TBS: the spec says "the TBS of this message ... {1, 2, ..., 125} byte(s)", 7 bits. Assumed here
to be the length of the whole MAC PDU in bytes, including the header and the TBS field itself. On
pack a None TBS is computed; for the paging messages a larger explicit TBS is allowed (the extra
bytes are zero Fill Bits, "future extensions"); everywhere else it must equal the PDU length.
Parse requires the bit string to be exactly 8*TBS bits (the PHY hands over one transport block).

Interpretations (the spec is silent or ambiguous, see the report):
  * Child fields of the D2R Scheduling Info are concatenated in the row order of Table 6.2.1.6-1:
    Time Resource Indication, Bit Duration, Frequency Resource Indication (Broadcast or Unicast),
    Block Repetition number, Channel Coding Indicator, Interval Bits, Sequence Length Indicator,
    Additional Midamble Indicator, D2R TBS. Codepoint k of a field is the k-th listed value.
  * Channel Coding Indicator {FEC, no FEC}: codepoint 0 is FEC, which is r_code True; Sequence
    Length {short, long}: 0 is short (7), 1 long (31); Additional Midamble {absent, present}.
  * A Frequency Resource Indication_Broadcast bitmap has at least one bit set (N_SFS >= 1) and
    exactly one in CFA paging; the Unicast value, like the bitmap bits, must be allowed for the bit
    duration. All 3-bit D2R TBS codepoints 125, 126, 127 are "not used in this release" (codepoint c means c + 1 bytes).
  * The Random ID Response and NACK Feedback lists have at least one entry (RIDR at most 8). ID
    entries of the Random ID Response are packed back to back, not byte aligned, as in Figure
    6.2.1.3-1. After the last entry 0 to 7 Fill Bits follow (the parser stops at fewer than 17 bits
    left, the smallest possible entry; 8 to 16 left bits are malformed).
  * R fields are written as 0 and ignored on parse (stated for R2D, assumed for D2R). Fill Bits and
    MAC Padding values are written as 0 and ignored on parse.
  * D2R padding is a whole number of bytes; the D2R message has no TBS field, so the TBS comes
    from the Scheduling Info (see D2RUpperLayerDataTransfer.for_tbs and parse_d2r(d2r_tbs=...)).
  * parse_d2r needs to be told which message it is (a 16-bit string is both a valid Msg1 and a
    2-byte D2R data message).
"""

from dataclasses import dataclass
from fractions import Fraction
from typing import List, Optional, Sequence, Tuple, Union

from ..phy.params import (BIT_DURATIONS, R_SFS_VALUES, D2RParams, allowed_r_sfs, i_bit_values)

Bits = List[int]

R2D_PAGING = 0b001
R2D_ACCESS_TRIGGER = 0b010
R2D_RANDOM_ID_RESPONSE = 0b011
R2D_UPPER_LAYER_DATA = 0b100
R2D_NACK_FEEDBACK = 0b101
D2R_UPPER_LAYER_DATA = 0b00

MAX_TBS = 125
MAX_ID_ENTRIES = 8
_RIDR_ENTRY_MIN_BITS = 17        # Echoed Random ID (16) + AIPI (1)


# ----------------------------------------------------------------------------------------------
# Bit helpers
# ----------------------------------------------------------------------------------------------

def _int_to_bits(value: int, width: int, name: str) -> Bits:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if not 0 <= value < (1 << width):
        raise ValueError(f"{name} = {value} does not fit in {width} bits (0..{(1 << width) - 1})")
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


def _bits_to_int(bits: Sequence[int]) -> int:
    v = 0
    for b in bits:
        v = (v << 1) | b
    return v


def _clean_bits(bits: Sequence[int], what: str = "bit string") -> Bits:
    try:
        out = list(bits)
    except TypeError:
        raise ValueError(f"{what} must be a sequence of 0/1 ints") from None
    for b in out:
        if isinstance(b, bool) or not isinstance(b, int) or b not in (0, 1):
            raise ValueError(f"{what} contains {b!r}, only 0 and 1 are allowed")
    return out


def _flag(value, name: str) -> int:
    if value not in (0, 1, True, False):
        raise ValueError(f"{name} must be 0/1 or a bool, got {value!r}")
    return int(value)


def _bytes_to_bits(data: bytes) -> Bits:
    return [(byte >> (7 - i)) & 1 for byte in data for i in range(8)]


def _bits_to_bytes(bits: Sequence[int]) -> bytes:
    assert len(bits) % 8 == 0
    return bytes(_bits_to_int(bits[i:i + 8]) for i in range(0, len(bits), 8))


def _check_bytes(value, name: str) -> bytes:
    if not isinstance(value, (bytes, bytearray)):
        raise ValueError(f"{name} must be bytes, got {type(value).__name__}")
    return bytes(value)


class _Reader:
    """Sequential MSB-first reader that raises ValueError on underrun."""

    def __init__(self, bits: Bits, what: str):
        self.bits, self.pos, self.what = bits, 0, what

    def left(self) -> int:
        return len(self.bits) - self.pos

    def take(self, n: int, name: str) -> int:
        if self.left() < n:
            raise ValueError(f"{self.what}: truncated, field {name} needs {n} bits but only "
                             f"{self.left()} are left")
        v = _bits_to_int(self.bits[self.pos:self.pos + n])
        self.pos += n
        return v

    def take_bits(self, n: int, name: str) -> Bits:
        if self.left() < n:
            raise ValueError(f"{self.what}: truncated, field {name} needs {n} bits but only "
                             f"{self.left()} are left")
        out = self.bits[self.pos:self.pos + n]
        self.pos += n
        return out


# ----------------------------------------------------------------------------------------------
# D2R Scheduling Info (6.2.1.6, Table 6.2.1.6-1, PDF p.20-22)
# ----------------------------------------------------------------------------------------------

SCHED_PAGING_CBRA = "paging_cbra"            # 18 bits
SCHED_PAGING_CFA = "paging_cfa"              # 24 bits
SCHED_RANDOM_ID_RESPONSE = "random_id_response"  # 24 bits
SCHED_R2D_DATA = "r2d_data"                  # 19 bits
SCHED_VARIANTS = (SCHED_PAGING_CBRA, SCHED_PAGING_CFA, SCHED_RANDOM_ID_RESPONSE, SCHED_R2D_DATA)

# TRI 1, BitDur 3, FreqBroadcast 8 / FreqUnicast 3, Block 1, Coding 1, Interval 2, Seq 1, AddMid 1, TBS 7
SCHED_WIDTH = {
    SCHED_PAGING_CBRA: 1 + 3 + 8 + 1 + 1 + 2 + 1 + 1,        # 18, no D2R TBS
    SCHED_PAGING_CFA: 3 + 8 + 1 + 1 + 2 + 1 + 1 + 7,         # 24, no Time Resource Indication
    SCHED_RANDOM_ID_RESPONSE: 3 + 8 + 1 + 1 + 2 + 1 + 1 + 7,  # 24
    SCHED_R2D_DATA: 3 + 3 + 1 + 1 + 2 + 1 + 1 + 7,           # 19, unicast frequency, no TRI
}


@dataclass
class SchedulingInfo:
    """The D2R Scheduling Info field, one dataclass for the four variants (see SCHED_VARIANTS).

    variant        which message carries it, decides the child fields present
    t_bit_tau      Bit Duration as a multiple of tau (one of params.BIT_DURATIONS)
    r_sfs          small frequency shift factors: the set marked in the Broadcast bitmap, or the
                   single Unicast value; ascending, from params.R_SFS_VALUES
    x              Time Resource Indication, 1 or 2 (CBRA paging only, else None)
    r_block        block repetition number, 1 or 2
    r_code         True when FEC is used (codepoint 0 of {FEC, no FEC})
    i_bit          interval in bits between midambles, already scaled by S
    l_amble        7 (short) or 31 (long)
    i_add          additional midamble present
    d2r_tbs        D2R TBS in bytes, 1..125 (None in CBRA paging)
    """
    variant: str
    t_bit_tau: Fraction
    r_sfs: Tuple[int, ...]
    r_block: int = 1
    r_code: bool = False
    i_bit: int = 0
    l_amble: int = 31
    i_add: bool = False
    d2r_tbs: Optional[int] = None
    x: Optional[int] = None

    def __post_init__(self):
        if self.variant not in SCHED_VARIANTS:
            raise ValueError(f"unknown D2R Scheduling Info variant {self.variant!r}; one of {SCHED_VARIANTS}")
        try:
            self.t_bit_tau = Fraction(self.t_bit_tau)
        except (TypeError, ValueError):
            raise ValueError(f"bit duration {self.t_bit_tau!r} is not a number") from None
        self.r_sfs = tuple(self.r_sfs)
        if self.i_bit == 0 and self.t_bit_tau in BIT_DURATIONS:
            self.i_bit = i_bit_values(self.t_bit_tau)[0]      # convenience default: codepoint 0
        self.validate()

    # -- validity ------------------------------------------------------------------------------
    def validate(self) -> None:
        v, t = self.variant, self.t_bit_tau
        if t not in BIT_DURATIONS:
            raise ValueError(f"bit duration {t}*tau is not one of {[str(b) for b in BIT_DURATIONS]}")
        if v == SCHED_PAGING_CBRA:
            if self.x not in (1, 2):
                raise ValueError(f"Time Resource Indication X must be 1 or 2 in CBRA paging, got {self.x!r}")
            if self.d2r_tbs is not None:
                raise ValueError("D2R TBS is absent from the Scheduling Info of CBRA paging; set d2r_tbs=None")
        else:
            if self.x is not None:
                raise ValueError(f"Time Resource Indication is only present in CBRA paging, not in {v}; set x=None")
            if isinstance(self.d2r_tbs, bool) or not isinstance(self.d2r_tbs, int) \
                    or not 1 <= self.d2r_tbs <= MAX_TBS:
                raise ValueError(f"D2R TBS must be an integer 1..{MAX_TBS} bytes (codepoints 125..127 are not used "
                                 f"in this release), got {self.d2r_tbs!r}")
        if not self.r_sfs:
            raise ValueError("at least one small frequency shift factor (R_SFS) must be indicated")
        if len(set(self.r_sfs)) != len(self.r_sfs) or list(self.r_sfs) != sorted(self.r_sfs):
            raise ValueError(f"R_SFS values must be unique and ascending, got {self.r_sfs}")
        ok = allowed_r_sfs(t)
        for r in self.r_sfs:
            if r not in R_SFS_VALUES:
                raise ValueError(f"R_SFS {r} is not one of {R_SFS_VALUES}")
            if r not in ok and v != SCHED_R2D_DATA:
                # 38.391 gates the values by bit duration only for the Broadcast field; a Unicast
                # value outside that set is a valid message, which D2RParams then refuses to use
                raise ValueError(f"R_SFS {r} may not be indicated with bit duration {t}*tau; allowed: {ok}")
        if v in (SCHED_PAGING_CFA, SCHED_R2D_DATA) and len(self.r_sfs) != 1:
            raise ValueError(f"{v} indicates exactly one R_SFS value, got {self.r_sfs}")
        if self.r_block not in (1, 2):
            raise ValueError(f"block repetition number is 1 or 2, got {self.r_block!r}")
        if self.i_bit not in i_bit_values(t):
            raise ValueError(f"I_bit {self.i_bit} is not allowed with bit duration {t}*tau; "
                             f"allowed: {i_bit_values(t)}")
        if self.l_amble not in (7, 31):
            raise ValueError(f"sequence length is 7 (short) or 31 (long), got {self.l_amble!r}")
        for name in ("r_code", "i_add"):
            if getattr(self, name) not in (True, False):
                raise ValueError(f"{name} must be a bool, got {getattr(self, name)!r}")

    # -- derived L1 values ---------------------------------------------------------------------
    @property
    def width(self) -> int:
        return SCHED_WIDTH[self.variant]

    @property
    def n_sfs(self) -> int:
        """Number of indicated small frequency shift factors (bits set in the bitmap)."""
        return len(self.r_sfs)

    @property
    def scale(self) -> int:
        """The scale factor S of the Interval Bits (1 at 2*tau ... 192 at tau/96)."""
        return i_bit_values(self.t_bit_tau)[0] // 48

    @property
    def i_bit_codepoint(self) -> int:
        return i_bit_values(self.t_bit_tau).index(self.i_bit)

    @property
    def d2r_tbs_bytes(self) -> Optional[int]:
        return self.d2r_tbs

    # -- pack / parse --------------------------------------------------------------------------
    def pack(self) -> Bits:
        self.validate()
        t = self.t_bit_tau
        out: Bits = []
        if self.variant == SCHED_PAGING_CBRA:
            out += _int_to_bits(self.x - 1, 1, "Time Resource Indication")
        out += _int_to_bits(BIT_DURATIONS.index(t), 3, "Bit Duration")
        if self.variant == SCHED_R2D_DATA:
            out += _int_to_bits(R_SFS_VALUES.index(self.r_sfs[0]), 3, "Frequency Resource Indication (Unicast)")
        else:
            out += [1 if r in self.r_sfs else 0 for r in R_SFS_VALUES]
        out += _int_to_bits(self.r_block - 1, 1, "Block Repetition number")
        out += [0 if self.r_code else 1]                       # {FEC, no FEC}
        out += _int_to_bits(self.i_bit_codepoint, 2, "Interval Bits")
        out += [1 if self.l_amble == 31 else 0]                # {short, long}
        out += [1 if self.i_add else 0]                        # {absent, present}
        if self.variant != SCHED_PAGING_CBRA:
            out += _int_to_bits(self.d2r_tbs - 1, 7, "D2R TBS")   # first codepoint = first value, 1 byte
        assert len(out) == self.width
        return out

    @classmethod
    def parse(cls, bits: Sequence[int], variant: str) -> "SchedulingInfo":
        if variant not in SCHED_VARIANTS:
            raise ValueError(f"unknown D2R Scheduling Info variant {variant!r}; one of {SCHED_VARIANTS}")
        bits = _clean_bits(bits, "D2R Scheduling Info")
        if len(bits) != SCHED_WIDTH[variant]:
            raise ValueError(f"D2R Scheduling Info of {variant} is {SCHED_WIDTH[variant]} bits, got {len(bits)}")
        r = _Reader(bits, f"D2R Scheduling Info ({variant})")
        x = r.take(1, "Time Resource Indication") + 1 if variant == SCHED_PAGING_CBRA else None
        t = BIT_DURATIONS[r.take(3, "Bit Duration")]
        if variant == SCHED_R2D_DATA:
            sfs: Tuple[int, ...] = (R_SFS_VALUES[r.take(3, "Frequency Resource Indication (Unicast)")],)
        else:
            bitmap = r.take_bits(8, "Frequency Resource Indication (Broadcast)")
            sfs = tuple(v for v, b in zip(R_SFS_VALUES, bitmap) if b)
        r_block = r.take(1, "Block Repetition number") + 1
        r_code = r.take(1, "Channel Coding Indicator") == 0
        i_bit = i_bit_values(t)[r.take(2, "Interval Bits")]
        l_amble = 31 if r.take(1, "Sequence Length Indicator") else 7
        i_add = bool(r.take(1, "Additional Midamble Indicator"))
        tbs = None
        if variant != SCHED_PAGING_CBRA:
            cp = r.take(7, "D2R TBS")
            tbs = cp + 1            # §6.2.1.6: the first codepoint is the first value of {1, ..., 125}
            if tbs > MAX_TBS:
                raise ValueError(f"D2R TBS codepoint {cp} is not used in this release (valid 0..{MAX_TBS - 1})")
        return cls(variant=variant, t_bit_tau=t, r_sfs=sfs, r_block=r_block, r_code=r_code, i_bit=i_bit,
                   l_amble=l_amble, i_add=i_add, d2r_tbs=tbs, x=x)


def parse_sched_info(bits: Sequence[int], variant: str) -> SchedulingInfo:
    return SchedulingInfo.parse(bits, variant)


def to_d2r_params(sched: SchedulingInfo, r_sfs: int, modulation: str = "bpsk") -> D2RParams:
    """The L1 parameters of one D2R transmission: the Scheduling Info plus the device's own R_SFS
    (one of the values the field indicates) and modulation (not signalled, 38.291 §7.1.3)."""
    sched.validate()
    if r_sfs not in sched.r_sfs:
        raise ValueError(f"R_SFS {r_sfs} is not one of the values indicated by the Scheduling Info {sched.r_sfs}")
    return D2RParams(t_bit_tau=sched.t_bit_tau, r_sfs=r_sfs, r_block=sched.r_block, r_code=sched.r_code,
                     l_amble=sched.l_amble, i_bit=sched.i_bit, i_add=sched.i_add, modulation=modulation)


# ----------------------------------------------------------------------------------------------
# R2D messages
# ----------------------------------------------------------------------------------------------

def _tbs_bits(tbs: int) -> Bits:
    return _int_to_bits(tbs, 7, "R2D TBS")


def _pad_to(bits: Bits, tbs: int) -> Bits:
    return bits + [0] * (8 * tbs - len(bits))


class _R2DBase:
    """Shared TBS handling. Subclasses implement _check(), _body() (no fill) and set
    _FILL_LARGER (True when TBS may exceed the natural length)."""

    _FILL_LARGER = False

    def _post(self):
        self._check()
        nat = -(-len(self._body(0)) // 8)
        if self.r2d_tbs is None:
            self.r2d_tbs = nat
        self._check_tbs()

    def _check_tbs(self):
        nat = -(-len(self._body(0)) // 8)
        t = self.r2d_tbs
        if isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= MAX_TBS:
            raise ValueError(f"R2D TBS must be an integer 1..{MAX_TBS} bytes, got {t!r}")
        if nat > MAX_TBS:
            raise ValueError(f"{type(self).__name__} is {nat} bytes, more than the maximum TBS {MAX_TBS}")
        if t < nat or (t > nat and not self._FILL_LARGER):
            raise ValueError(f"R2D TBS {t} does not match the {type(self).__name__} length of {nat} bytes")

    def pack(self) -> Bits:
        self._check()
        self._check_tbs()
        return _pad_to(self._body(self.r2d_tbs), self.r2d_tbs)


def _paging_id(value, present: bool):
    if value is None:
        return None
    ids = _clean_bits(value, "Paging ID")
    if len(ids) > 255:
        raise ValueError(f"Paging ID is {len(ids)} bits, the 8-bit Paging ID Length allows at most 255")
    return tuple(ids)


@dataclass
class AIoTPagingCBRA(_R2DBase):
    """A-IoT Paging indicating CBRA (AT = 1), Figure 6.2.1.1-1.

    Field order and widths: type 001 (3), R2D TBS (7), AT=1 (1), Transaction ID (6), PIPI (1),
    [Paging ID Length (8), Paging ID (length bits) when PIPI = 1], Number of Access Occasions (4),
    D2R Scheduling Info (18), K (1), Fill Bits (0..7, or more for extensions).
    paging_id None means PIPI = 0. num_access_occasions_code n means 2**n access occasions.
    k_bit 0 -> K = 1, 1 -> K = 4.
    """
    transaction_id: int
    sched: SchedulingInfo
    paging_id: Optional[Sequence[int]] = None
    num_access_occasions_code: int = 0
    k_bit: int = 0
    r2d_tbs: Optional[int] = None
    _FILL_LARGER = True

    def __post_init__(self):
        self.paging_id = _paging_id(self.paging_id, True)
        self._post()

    @property
    def num_access_occasions(self) -> int:
        return 1 << self.num_access_occasions_code

    @property
    def k(self) -> int:
        return 4 if self.k_bit else 1

    def _check(self):
        _int_to_bits(self.transaction_id, 6, "Transaction ID")
        _int_to_bits(self.num_access_occasions_code, 4, "Number of Access Occasions")
        _flag(self.k_bit, "K")
        if not isinstance(self.sched, SchedulingInfo) or self.sched.variant != SCHED_PAGING_CBRA:
            raise ValueError("CBRA paging needs a SchedulingInfo of variant 'paging_cbra'")
        self.paging_id = _paging_id(self.paging_id, True)

    def _body(self, tbs: int) -> Bits:
        b = _int_to_bits(R2D_PAGING, 3, "type") + _tbs_bits(tbs) + [1]
        b += _int_to_bits(self.transaction_id, 6, "Transaction ID")
        if self.paging_id is None:
            b += [0]
        else:
            b += [1] + _int_to_bits(len(self.paging_id), 8, "Paging ID Length") + list(self.paging_id)
        b += _int_to_bits(self.num_access_occasions_code, 4, "Number of Access Occasions")
        b += self.sched.pack() + [_flag(self.k_bit, "K")]
        return b


@dataclass
class AIoTPagingCFA(_R2DBase):
    """A-IoT Paging indicating CFA (AT = 0), Figure 6.2.1.1-2.

    Field order and widths: type 001 (3), R2D TBS (7), AT=0 (1), Paging ID Length (8), Paging ID
    (length bits), D2R Scheduling Info (24), Fill Bits. There is no PIPI, Transaction ID, Number of
    Access Occasions or K in the CFA form.
    """
    paging_id: Sequence[int]
    sched: SchedulingInfo
    r2d_tbs: Optional[int] = None
    _FILL_LARGER = True

    def __post_init__(self):
        self._post()

    def _check(self):
        if self.paging_id is None:
            raise ValueError("CFA paging always carries a Paging ID")
        self.paging_id = _paging_id(self.paging_id, True)
        if not isinstance(self.sched, SchedulingInfo) or self.sched.variant != SCHED_PAGING_CFA:
            raise ValueError("CFA paging needs a SchedulingInfo of variant 'paging_cfa'")

    def _body(self, tbs: int) -> Bits:
        b = _int_to_bits(R2D_PAGING, 3, "type") + _tbs_bits(tbs) + [0]
        b += _int_to_bits(len(self.paging_id), 8, "Paging ID Length") + list(self.paging_id)
        return b + self.sched.pack()


@dataclass
class AccessTrigger:
    """Access Trigger, Figure 6.2.1.2-1: only the 3-bit type 010. Not byte aligned (6.1.1)."""

    def pack(self) -> Bits:
        return _int_to_bits(R2D_ACCESS_TRIGGER, 3, "type")


@dataclass
class RandomIDEntry:
    """One ID entry of the Random ID Response: [Frequency Index (3)], Echoed Random ID (16),
    AIPI (1), [Assigned AS ID (16)]. freq_index is a codepoint into R_SFS_VALUES (0 -> 1, 7 -> 128)."""
    echoed_random_id: int
    assigned_as_id: Optional[int] = None
    freq_index: Optional[int] = None

    def validate(self):
        _int_to_bits(self.echoed_random_id, 16, "Echoed Random ID")
        if self.assigned_as_id is not None:
            _int_to_bits(self.assigned_as_id, 16, "Assigned AS ID")
        if self.freq_index is not None:
            _int_to_bits(self.freq_index, 3, "Frequency Index")

    @property
    def r_sfs(self) -> Optional[int]:
        return None if self.freq_index is None else R_SFS_VALUES[self.freq_index]


@dataclass
class RandomIDResponse(_R2DBase):
    """Random ID Response (Msg2), Figure 6.2.1.3-1.

    Field order and widths: type 011 (3), R2D TBS (7), D2R Scheduling Info (24), FIPI (1), then 1..8
    ID entries back to back, each [Frequency Index (3) if FIPI], Echoed Random ID (16), AIPI (1),
    [Assigned AS ID (16) if AIPI], then Fill Bits (0..7). FIPI is derived from the entries: either
    all have a freq_index or none does. TBS must equal the PDU length.
    """
    sched: SchedulingInfo
    entries: Sequence[RandomIDEntry]
    r2d_tbs: Optional[int] = None

    def __post_init__(self):
        self.entries = tuple(self.entries)
        self._post()

    @property
    def fipi(self) -> bool:
        return bool(self.entries) and self.entries[0].freq_index is not None

    def _check(self):
        if not isinstance(self.sched, SchedulingInfo) or self.sched.variant != SCHED_RANDOM_ID_RESPONSE:
            raise ValueError("Random ID Response needs a SchedulingInfo of variant 'random_id_response'")
        self.entries = tuple(self.entries)
        if not 1 <= len(self.entries) <= MAX_ID_ENTRIES:
            raise ValueError(f"Random ID Response carries 1..{MAX_ID_ENTRIES} ID entries, got {len(self.entries)}")
        for e in self.entries:
            if not isinstance(e, RandomIDEntry):
                raise ValueError("entries must be RandomIDEntry objects")
            e.validate()
            if (e.freq_index is not None) != self.fipi:
                raise ValueError("FIPI applies to all ID entries: either every entry has a Frequency Index or none")

    def _body(self, tbs: int) -> Bits:
        b = _int_to_bits(R2D_RANDOM_ID_RESPONSE, 3, "type") + _tbs_bits(tbs) + self.sched.pack()
        b.append(1 if self.fipi else 0)
        for e in self.entries:
            if self.fipi:
                b += _int_to_bits(e.freq_index, 3, "Frequency Index")
            b += _int_to_bits(e.echoed_random_id, 16, "Echoed Random ID")
            if e.assigned_as_id is None:
                b.append(0)
            else:
                b.append(1)
                b += _int_to_bits(e.assigned_as_id, 16, "Assigned AS ID")
        return b


@dataclass
class R2DUpperLayerDataTransfer(_R2DBase):
    """R2D Upper Layer Data Transfer, Figures 6.2.1.4-1 and -2.

    Field order and widths: type 100 (3), R2D TBS (7), AS ID (16), D2R Scheduling Info (19), CI (1),
    then CI = 1: R (2), Data SDU (to the end; TBS - 6 bytes); CI = 0: R (3), Received Data Size (7)
    (always 7 bytes). Exactly one of data_sdu / received_data_size is set.
    """
    as_id: int
    sched: SchedulingInfo
    data_sdu: Optional[bytes] = None
    received_data_size: Optional[int] = None
    r2d_tbs: Optional[int] = None

    def __post_init__(self):
        self._post()

    @property
    def ci(self) -> int:
        return 1 if self.data_sdu is not None else 0

    def _check(self):
        _int_to_bits(self.as_id, 16, "AS ID")
        if not isinstance(self.sched, SchedulingInfo) or self.sched.variant != SCHED_R2D_DATA:
            raise ValueError("R2D Upper Layer Data Transfer needs a SchedulingInfo of variant 'r2d_data'")
        if (self.data_sdu is None) == (self.received_data_size is None):
            raise ValueError("set exactly one of data_sdu (CI = 1) and received_data_size (CI = 0)")
        if self.data_sdu is not None:
            self.data_sdu = _check_bytes(self.data_sdu, "Data SDU")
        else:
            _int_to_bits(self.received_data_size, 7, "Received Data Size")

    def _body(self, tbs: int) -> Bits:
        b = _int_to_bits(R2D_UPPER_LAYER_DATA, 3, "type") + _tbs_bits(tbs)
        b += _int_to_bits(self.as_id, 16, "AS ID") + self.sched.pack()
        if self.data_sdu is not None:
            return b + [1, 0, 0] + _bytes_to_bits(self.data_sdu)
        return b + [0, 0, 0, 0] + _int_to_bits(self.received_data_size, 7, "Received Data Size")


@dataclass
class NACKFeedback(_R2DBase):
    """NACK Feedback, Figure 6.2.1.5-1.

    Field order and widths: type 101 (3), R2D TBS (7), R (6), then one or more AS ID (16). TBS =
    2 + 2 * number of AS IDs bytes.
    """
    as_ids: Sequence[int]
    r2d_tbs: Optional[int] = None

    def __post_init__(self):
        self.as_ids = tuple(self.as_ids)
        self._post()

    def _check(self):
        self.as_ids = tuple(self.as_ids)
        if not self.as_ids:
            raise ValueError("NACK Feedback carries at least one AS ID")
        for a in self.as_ids:
            _int_to_bits(a, 16, "AS ID")

    def _body(self, tbs: int) -> Bits:
        b = _int_to_bits(R2D_NACK_FEEDBACK, 3, "type") + _tbs_bits(tbs) + [0] * 6
        for a in self.as_ids:
            b += _int_to_bits(a, 16, "AS ID")
        return b


R2DMessage = Union[AIoTPagingCBRA, AIoTPagingCFA, AccessTrigger, RandomIDResponse,
                   R2DUpperLayerDataTransfer, NACKFeedback]


def parse_r2d(bits: Sequence[int]) -> R2DMessage:
    """Parse one R2D MAC PDU. All messages but Access Trigger (exactly 3 bits) must be exactly
    8 * R2D TBS bits long."""
    bits = _clean_bits(bits, "R2D PDU")
    if len(bits) < 3:
        raise ValueError(f"R2D PDU of {len(bits)} bits is too short for the 3-bit message type")
    mtype = _bits_to_int(bits[:3])
    if mtype in (0b000, 0b110, 0b111):
        raise ValueError(f"R2D message type {mtype:03b} is reserved")
    if mtype == R2D_ACCESS_TRIGGER:
        if len(bits) != 3:
            raise ValueError(f"Access Trigger is exactly the 3-bit message type, got {len(bits)} bits")
        return AccessTrigger()
    if len(bits) % 8:
        raise ValueError(f"R2D PDU of {len(bits)} bits is not byte aligned")
    r = _Reader(bits, "R2D PDU")
    r.take(3, "R2D Message Type")
    tbs = r.take(7, "R2D TBS")
    if not 1 <= tbs <= MAX_TBS:
        raise ValueError(f"R2D TBS {tbs} is outside 1..{MAX_TBS}")
    if len(bits) != 8 * tbs:
        raise ValueError(f"R2D TBS says {tbs} bytes but the PDU is {len(bits)} bits ({len(bits) / 8:g} bytes)")

    if mtype == R2D_PAGING:
        at = r.take(1, "Access Type")
        if at == 1:
            tid = r.take(6, "Transaction ID")
            pid = None
            if r.take(1, "PIPI"):
                n = r.take(8, "Paging ID Length")
                pid = r.take_bits(n, "Paging ID")
            noa = r.take(4, "Number of Access Occasions")
            sched = SchedulingInfo.parse(r.take_bits(SCHED_WIDTH[SCHED_PAGING_CBRA], "D2R Scheduling Info"),
                                         SCHED_PAGING_CBRA)
            k = r.take(1, "K")
            return AIoTPagingCBRA(transaction_id=tid, sched=sched, paging_id=pid,
                                  num_access_occasions_code=noa, k_bit=k, r2d_tbs=tbs)
        n = r.take(8, "Paging ID Length")
        pid = r.take_bits(n, "Paging ID")
        sched = SchedulingInfo.parse(r.take_bits(SCHED_WIDTH[SCHED_PAGING_CFA], "D2R Scheduling Info"),
                                     SCHED_PAGING_CFA)
        return AIoTPagingCFA(paging_id=pid, sched=sched, r2d_tbs=tbs)

    if mtype == R2D_RANDOM_ID_RESPONSE:
        sched = SchedulingInfo.parse(r.take_bits(SCHED_WIDTH[SCHED_RANDOM_ID_RESPONSE], "D2R Scheduling Info"),
                                     SCHED_RANDOM_ID_RESPONSE)
        fipi = r.take(1, "FIPI")
        entries = []
        while r.left() >= _RIDR_ENTRY_MIN_BITS + 3 * fipi:
            fi = r.take(3, "Frequency Index") if fipi else None
            echoed = r.take(16, "Echoed Random ID")
            aid = r.take(16, "Assigned AS ID") if r.take(1, "AIPI") else None
            entries.append(RandomIDEntry(echoed_random_id=echoed, assigned_as_id=aid, freq_index=fi))
            if len(entries) > MAX_ID_ENTRIES:
                raise ValueError(f"Random ID Response carries more than {MAX_ID_ENTRIES} ID entries")
        if r.left() >= 8:
            raise ValueError(f"Random ID Response: {r.left()} bits left after the last ID entry, "
                             f"but Fill Bits are 0..7 bits")
        return RandomIDResponse(sched=sched, entries=entries, r2d_tbs=tbs)

    if mtype == R2D_UPPER_LAYER_DATA:
        as_id = r.take(16, "AS ID")
        sched = SchedulingInfo.parse(r.take_bits(SCHED_WIDTH[SCHED_R2D_DATA], "D2R Scheduling Info"),
                                     SCHED_R2D_DATA)
        if r.take(1, "CI"):
            r.take(2, "R")
            sdu = _bits_to_bytes(r.take_bits(r.left(), "Data SDU"))
            return R2DUpperLayerDataTransfer(as_id=as_id, sched=sched, data_sdu=sdu, r2d_tbs=tbs)
        r.take(3, "R")
        rds = r.take(7, "Received Data Size")
        if r.left():
            raise ValueError(f"R2D Upper Layer Data Transfer with CI = 0 is 7 bytes, {r.left()} bits trail")
        return R2DUpperLayerDataTransfer(as_id=as_id, sched=sched, received_data_size=rds, r2d_tbs=tbs)

    # R2D_NACK_FEEDBACK
    r.take(6, "R")
    ids = []
    while r.left():
        ids.append(r.take(16, "AS ID"))
    if not ids:
        raise ValueError("NACK Feedback carries at least one AS ID")
    return NACKFeedback(as_ids=ids, r2d_tbs=tbs)


# ----------------------------------------------------------------------------------------------
# D2R messages
# ----------------------------------------------------------------------------------------------

@dataclass
class AccessRandomID:
    """Access Random ID (Msg1), Figure 6.2.2.1-1: just the 16-bit Random ID, no type field."""
    random_id: int

    def pack(self) -> Bits:
        return _int_to_bits(self.random_id, 16, "Random ID")


@dataclass
class D2RUpperLayerDataTransfer:
    """D2R Upper Layer Data Transfer, Figure 6.2.2.2-1.

    Field order and widths: type 00 (2), R (6, zero), MDI (1), SDU Length (7, bytes), Data SDU,
    MAC Padding (optional, padding_bytes bytes of zeros). Total length 2 + len(sdu) + padding_bytes
    bytes, at most 125 (the largest D2R TBS). There is no TBS field; use for_tbs() to pad to a TBS.
    """
    sdu: bytes = b""
    mdi: int = 0
    padding_bytes: int = 0

    def __post_init__(self):
        self._check()

    def _check(self):
        self.sdu = _check_bytes(self.sdu, "Data SDU")
        _flag(self.mdi, "More Data Indication")
        _int_to_bits(len(self.sdu), 7, "SDU Length")
        if isinstance(self.padding_bytes, bool) or not isinstance(self.padding_bytes, int) \
                or self.padding_bytes < 0:
            raise ValueError(f"padding_bytes must be a non-negative integer, got {self.padding_bytes!r}")
        if self.length_bytes > MAX_TBS:
            raise ValueError(f"D2R Upper Layer Data Transfer of {self.length_bytes} bytes exceeds the "
                             f"maximum D2R TBS {MAX_TBS}")

    @property
    def length_bytes(self) -> int:
        return 2 + len(self.sdu) + self.padding_bytes

    @classmethod
    def for_tbs(cls, sdu: bytes, tbs: int, mdi: int = 0) -> "D2RUpperLayerDataTransfer":
        """A message padded to exactly tbs bytes (5.4.2: MAC Padding is included if the PDU is
        smaller than the D2R TBS)."""
        if isinstance(tbs, bool) or not isinstance(tbs, int) or not 1 <= tbs <= MAX_TBS:
            raise ValueError(f"D2R TBS must be 1..{MAX_TBS} bytes, got {tbs!r}")
        pad = tbs - 2 - len(sdu)
        if pad < 0:
            raise ValueError(f"MAC PDU of {2 + len(sdu)} bytes does not fit the D2R TBS of {tbs} bytes")
        return cls(sdu=sdu, mdi=mdi, padding_bytes=pad)

    def pack(self) -> Bits:
        self._check()
        b = _int_to_bits(D2R_UPPER_LAYER_DATA, 2, "type") + [0] * 6
        b += [_flag(self.mdi, "MDI")] + _int_to_bits(len(self.sdu), 7, "SDU Length")
        return b + _bytes_to_bits(self.sdu) + [0] * (8 * self.padding_bytes)


D2RMessage = Union[AccessRandomID, D2RUpperLayerDataTransfer]
D2R_ACCESS_RANDOM_ID = "access_random_id"
D2R_UPPER_LAYER_DATA_KIND = "upper_layer_data"


def parse_d2r(bits: Sequence[int], kind: str, d2r_tbs: Optional[int] = None) -> D2RMessage:
    """Parse a D2R PDU of the given kind ("access_random_id" or "upper_layer_data"). The two cannot
    be told apart from the bits. For the data message d2r_tbs (bytes, from the Scheduling Info),
    when given, must equal the PDU length; the bytes after the SDU are the MAC Padding."""
    bits = _clean_bits(bits, "D2R PDU")
    if kind == D2R_ACCESS_RANDOM_ID:
        if len(bits) != 16:
            raise ValueError(f"Access Random ID is exactly 16 bits, got {len(bits)}")
        return AccessRandomID(random_id=_bits_to_int(bits))
    if kind != D2R_UPPER_LAYER_DATA_KIND:
        raise ValueError(f"unknown D2R message kind {kind!r}; use {D2R_ACCESS_RANDOM_ID!r} or "
                         f"{D2R_UPPER_LAYER_DATA_KIND!r}")
    if len(bits) % 8 or len(bits) < 16:
        raise ValueError(f"D2R Upper Layer Data Transfer is a whole number of bytes, at least 2; got {len(bits)} bits")
    if len(bits) > 8 * MAX_TBS:
        raise ValueError(f"D2R PDU of {len(bits) // 8} bytes exceeds the maximum D2R TBS {MAX_TBS}")
    if d2r_tbs is not None and len(bits) != 8 * d2r_tbs:
        raise ValueError(f"D2R TBS says {d2r_tbs} bytes but the PDU is {len(bits) / 8:g} bytes")
    r = _Reader(bits, "D2R PDU")
    mtype = r.take(2, "D2R Message Type")
    if mtype != D2R_UPPER_LAYER_DATA:
        raise ValueError(f"D2R message type {mtype:02b} is reserved")
    r.take(6, "R")
    mdi = r.take(1, "MDI")
    n = r.take(7, "SDU Length")
    if r.left() < 8 * n:
        raise ValueError(f"SDU Length {n} bytes but only {r.left() // 8} bytes follow")
    sdu = _bits_to_bytes(r.take_bits(8 * n, "Data SDU"))
    return D2RUpperLayerDataTransfer(sdu=sdu, mdi=mdi, padding_bytes=r.left() // 8)
