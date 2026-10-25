# tests/test_mac_pdu.py
"""Tests of OAI's C A-IoT MAC codec (aiot_mac_pdu.c in libaiot_phy.so, used through amber.mac) against
3GPP TS 38.391 V19.4.0 clause 6: the bit layouts are worked out by hand from the spec figures."""

import random
from fractions import Fraction

import pytest

from amber import mac
from amber.mac import (
    SCHED_PAGING_CBRA, SCHED_PAGING_CFA, SCHED_R2D_DATA, SCHED_RANDOM_ID_RESPONSE,
    PAGING_CBRA, PAGING_CFA, ACCESS_TRIGGER, RANDOM_ID_RESPONSE, R2D_DATA, NACK_FEEDBACK,
    D2R_ACCESS_RANDOM_ID, D2R_UPPER_LAYER_DATA)
from amber.phy.params import BIT_DURATIONS, R_SFS_VALUES, D2RParams, allowed_r_sfs, i_bit_values

N_ROUNDS = 300
SCHED_VARIANTS = (SCHED_PAGING_CBRA, SCHED_PAGING_CFA, SCHED_RANDOM_ID_RESPONSE, SCHED_R2D_DATA)


def b(s: str):
    """A bit string such as '001 0110' (spaces ignored) as a list of ints."""
    return [int(c) for c in s if c in "01"]


def rand_bits(rng, n):
    return [rng.randint(0, 1) for _ in range(n)]


# ----------------------------------------------------------------------------------------------
# What a C message holds, as plain values to compare
# ----------------------------------------------------------------------------------------------

def sv(s):
    """A Scheduling Info as (T_bit, R_SFS, X, R_block, coded, I_bit, L_amble, additional, D2R TBS); absent is None."""
    return (Fraction(s.t_bit_num, s.t_bit_den), tuple(s.shifts), s.x or None, s.r_block, bool(s.r_code), s.i_bit,
            s.l_amble, bool(s.i_add), s.d2r_tbs or None)


def pid_of(m):
    return tuple(m.paging_id[:m.paging_id_len]) if m.has_paging_id else None


def entry_of(e):
    return (e.echoed_random_id, None if e.assigned_as_id < 0 else e.assigned_as_id,
            None if e.freq_index < 0 else e.freq_index)


def sec_of(m):
    """SPPI and the Security Parameter of a paging: None for SPPI 0."""
    return tuple(m.security_param) if m.has_security_param else None


def view(m):
    """The fields of an R2D message that its kind uses."""
    if m.kind == ACCESS_TRIGGER:
        return (m.kind,)
    if m.kind == PAGING_CBRA:
        return (m.kind, m.r2d_tbs, sv(m.sched), m.transaction_id, m.num_access_occasions_code, m.k_bit, pid_of(m),
                sec_of(m))
    if m.kind == PAGING_CFA:
        return (m.kind, m.r2d_tbs, sv(m.sched), pid_of(m), sec_of(m))
    if m.kind == RANDOM_ID_RESPONSE:
        return (m.kind, m.r2d_tbs, sv(m.sched), tuple(entry_of(e) for e in m.entries[:m.n_entries]))
    if m.kind == R2D_DATA:
        return (m.kind, m.r2d_tbs, sv(m.sched), m.as_id,
                bytes(m.sdu[:m.sdu_len]) if m.has_sdu else None, None if m.has_sdu else m.received_data_size)
    return (m.kind, m.r2d_tbs, tuple(m.as_ids[:m.n_as_ids]))


def d2r_view(m):
    return (m.sdu, m.mdi, m.padding_bytes)


def finish(m, extra=0):
    """Pack m with the TBS the codec computes (plus extra Fill Bytes), set m.r2d_tbs to it, return the bits."""
    if m.kind != ACCESS_TRIGGER:
        m.r2d_tbs = 0
        m.r2d_tbs = min(125, len(mac.pack_r2d(m)) // 8 + extra)
    return mac.pack_r2d(m)


def rand_sched(rng, variant):
    t = rng.choice(BIT_DURATIONS)
    ok = allowed_r_sfs(t)
    if variant in (SCHED_PAGING_CFA, SCHED_R2D_DATA):
        sfs = (rng.choice(ok),)
    else:
        sfs = tuple(r for r in ok if rng.random() < 0.5) or (rng.choice(ok),)
    cbra = variant == SCHED_PAGING_CBRA
    return mac.sched(
        variant, sfs, d2r_tbs=0 if cbra else rng.randint(1, 125), x=rng.choice((1, 2)) if cbra else 0,
        t_bit_tau=t, r_block=rng.choice((1, 2)), r_code=rng.random() < .5, i_bit=rng.choice(i_bit_values(t)),
        l_amble=rng.choice((7, 31)), i_add=rng.random() < .5)


def rand_security(rng):
    """A Security Parameter (SPPI 1) half the time, else None (SPPI 0)."""
    return rand_bits(rng, mac.SECURITY_PARAM_BITS) if rng.random() < .5 else None


def rand_cbra(rng):
    pid = rand_bits(rng, rng.randint(0, 600)) if rng.random() < .6 else None
    return mac.paging_cbra(rng.randrange(64), rand_sched(rng, SCHED_PAGING_CBRA), rng.randrange(16),
                           rng.randint(0, 1), pid, security_param=rand_security(rng))


def rand_cfa(rng):
    return mac.paging_cfa(rand_bits(rng, rng.randint(0, 600)), rand_sched(rng, SCHED_PAGING_CFA),
                          security_param=rand_security(rng))


def rand_ridr(rng):
    fipi = rng.random() < .5
    ents = [(rng.randrange(1 << 16), rng.randrange(1 << 16) if rng.random() < .5 else None,
             rng.randrange(8) if fipi else None) for _ in range(rng.randint(1, 8))]
    return mac.random_id_response(ents, rand_sched(rng, SCHED_RANDOM_ID_RESPONSE))


def rand_r2d_data(rng):
    as_id, s = rng.randrange(1 << 16), rand_sched(rng, SCHED_R2D_DATA)
    if rng.random() < .5:
        return mac.r2d_data(as_id, s, sdu=bytes(rng.randrange(256) for _ in range(rng.randint(0, 119))))
    return mac.r2d_data(as_id, s, received=rng.randrange(126))


def rand_nack(rng):
    return mac.nack([rng.randrange(1 << 16) for _ in range(rng.randint(1, 61))])


R2D_GENERATORS = [rand_cbra, rand_cfa, rand_ridr, rand_r2d_data, rand_nack]


# ----------------------------------------------------------------------------------------------
# Round trips
# ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("gen", R2D_GENERATORS, ids=lambda g: g.__name__)
def test_r2d_round_trip(gen):
    rng = random.Random(0xA10 + len(gen.__name__))
    for _ in range(N_ROUNDS):
        msg = gen(rng)
        # extra zero Fill Bits ("future extensions") are allowed in paging
        bits = finish(msg, rng.randint(0, 3) if msg.kind in (PAGING_CBRA, PAGING_CFA) and rng.random() < .3 else 0)
        assert len(bits) == 8 * msg.r2d_tbs and set(bits) <= {0, 1}
        parsed = mac.parse_r2d(bits)
        assert parsed is not None and view(parsed) == view(msg)
        assert mac.pack_r2d(parsed) == bits


def test_access_trigger_is_three_bits():
    assert mac.pack_r2d(mac.access_trigger()) == [0, 1, 0]
    assert mac.parse_r2d([0, 1, 0]).kind == ACCESS_TRIGGER


@pytest.mark.parametrize("variant", SCHED_VARIANTS)
def test_sched_round_trip(variant):
    rng = random.Random(7)
    for _ in range(N_ROUNDS):
        s = rand_sched(rng, variant)
        bits = mac.sched_pack(s)
        assert len(bits) == mac.sched_width(variant)
        assert sv(mac.sched_parse(bits, variant)) == sv(s)


def test_d2r_round_trip():
    rng = random.Random(11)
    for _ in range(N_ROUNDS):
        rid = rng.randrange(1 << 16)
        assert mac.parse_d2r(mac.pack_d2r(mac.access_random_id(rid)), D2R_ACCESS_RANDOM_ID).random_id == rid
        sdu = bytes(rng.randrange(256) for _ in range(rng.randint(0, 100)))
        mdi, pad = rng.randint(0, 1), rng.randint(0, 120 - len(sdu))
        bits = mac.pack_d2r(mac.d2r_data(sdu, mdi=mdi, padding_bytes=pad))
        length = 2 + len(sdu) + pad
        assert len(bits) == 8 * length
        assert d2r_view(mac.parse_d2r(bits, D2R_UPPER_LAYER_DATA)) == (sdu, mdi, pad)
        assert d2r_view(mac.parse_d2r(bits, D2R_UPPER_LAYER_DATA, d2r_tbs=length)) == (sdu, mdi, pad)


# ----------------------------------------------------------------------------------------------
# Exact lengths, worked out from the spec figures
# ----------------------------------------------------------------------------------------------

def test_sched_widths_match_table_6_2_1_6_1():
    # Child field lengths: TRI 1, Bit Duration 3, Freq Broadcast 8 / Unicast 3, Block 1, Coding 1,
    # Interval 2, Sequence 1, Additional Midamble 1, D2R TBS 7.
    assert mac.sched_width(SCHED_PAGING_CBRA) == 1 + 3 + 8 + 1 + 1 + 2 + 1 + 1 == 18      # no D2R TBS
    assert mac.sched_width(SCHED_PAGING_CFA) == 3 + 8 + 1 + 1 + 2 + 1 + 1 + 7 == 24       # no TRI
    assert mac.sched_width(SCHED_RANDOM_ID_RESPONSE) == 24
    assert mac.sched_width(SCHED_R2D_DATA) == 3 + 3 + 1 + 1 + 2 + 1 + 1 + 7 == 19         # unicast, no TRI


def sched(variant, **kw):
    d = dict(t_bit_tau=Fraction(1, 8), r_sfs=(4,), i_bit=48 * 16)
    if variant != SCHED_PAGING_CBRA:
        d["d2r_tbs"] = 20
    else:
        d["x"] = 1
    d.update(kw)
    return mac.sched(variant, **d)


def sized(m, body=None):
    """(bits, TBS) of m once packed; the TBS is also what parsing the bits reports. body: its bits before Fill Bits."""
    bits = mac.pack_r2d(m)
    tbs = mac.parse_r2d(bits).r2d_tbs
    assert len(bits) == 8 * tbs
    if body is not None:
        assert mac.r2d_body_bits(m) == body
    return len(bits), tbs


def test_hand_computed_lengths():
    # CBRA paging, SPPI = 0, PIPI = 0: 3 type + 7 TBS + 1 SPPI + 1 AT + 6 TID + 1 PIPI + 4 NAO + 18 sched
    # + 1 K = 42 bits -> 6 fill bits -> 48 bits, TBS 6.
    assert sized(mac.paging_cbra(1, sched(SCHED_PAGING_CBRA)), 42) == (48, 6)
    # With a 20-bit Paging ID: 42 + 10 (length) + 20 = 72 bits, TBS 9, no fill.
    assert sized(mac.paging_cbra(1, sched(SCHED_PAGING_CBRA), paging_id=[1] * 20), 72) == (72, 9)
    # CFA paging, SPPI = 0, 32-bit Paging ID: 3 + 7 + 1 + 1 + 10 + 32 + 24 = 78 bits -> 80, TBS 10.
    assert sized(mac.paging_cfa([0] * 32, sched(SCHED_PAGING_CFA)), 78) == (80, 10)
    # SPPI = 1 adds the 128-bit Security Parameter: CBRA without Paging ID 42 + 128 = 170 bits -> 176, TBS 22;
    # with a 20-bit Paging ID 200 bits, TBS 25.
    sec = [0, 1] * 64
    assert sized(mac.paging_cbra(1, sched(SCHED_PAGING_CBRA), security_param=sec), 170) == (176, 22)
    assert sized(mac.paging_cbra(1, sched(SCHED_PAGING_CBRA), paging_id=[1] * 20, security_param=sec), 200) == (200, 25)
    # CFA, 16-bit Paging ID: 3 + 7 + 1 + 128 + 1 + 10 + 16 + 24 = 190 bits -> 192, TBS 24.
    assert sized(mac.paging_cfa([1] * 16, sched(SCHED_PAGING_CFA), security_param=sec), 190) == (192, 24)
    # Random ID Response, one entry without FI or AS ID: 3 + 7 + 24 + 1 FIPI + (16 + 1) = 52 -> 56, TBS 7.
    rs = sched(SCHED_RANDOM_ID_RESPONSE)
    assert sized(mac.random_id_response([(5, None, None)], rs), 52) == (56, 7)
    # One entry with FI and AS ID: 35 + (3 + 16 + 1 + 16) = 71 -> 72, TBS 9.
    assert sized(mac.random_id_response([(5, 6, 1)], rs), 71) == (72, 9)
    # Three such entries: 35 + 3 * 20 (FI, ID, AIPI, no AS ID) = 95 -> 96, TBS 12.
    assert sized(mac.random_id_response([(i, None, 0) for i in range(3)], rs), 95) == (96, 12)
    # Eight entries with everything: 35 + 8 * 36 = 323 -> 328, TBS 41.
    assert sized(mac.random_id_response([(i, i, 0) for i in range(8)], rs), 323) == (328, 41)
    # R2D data, CI = 1: 3 + 7 + 16 + 19 + 1 + 2 R = 48 bits + SDU; 5-byte SDU -> 88 bits, TBS 11.
    ds = sched(SCHED_R2D_DATA)
    assert sized(mac.r2d_data(1, ds, sdu=b"12345"), 88) == (88, 11)
    # CI = 0: 3 + 7 + 16 + 19 + 1 + 3 R + 7 Received Data Size = 56 bits, TBS 7 (the size is 0..125).
    assert sized(mac.r2d_data(1, ds, received=9), 56) == (56, 7)
    # NACK: 3 + 7 + 6 R = 16 bits + 16 per AS ID; 3 IDs -> 64 bits, TBS 8.
    assert sized(mac.nack([1, 2, 3]), 64) == (64, 8)
    # Access Trigger: 3 bits, not padded. Msg1: 16 bits. D2R data: 2 + 4 SDU + 3 padding = 9 bytes.
    assert len(mac.pack_r2d(mac.access_trigger())) == 3
    assert len(mac.pack_d2r(mac.access_random_id(7))) == 16
    assert len(mac.pack_d2r(mac.d2r_data(b"abcd", padding_bytes=3))) == 72


# ----------------------------------------------------------------------------------------------
# Hand-written bit strings, field by field from the spec figures
# ----------------------------------------------------------------------------------------------

# D2R Scheduling Info, R2D-data variant (19 bits): Bit Duration tau/8 = codepoint 4 '100';
# Unicast R_SFS 4 = index 2 '010'; Block 2 -> '1'; FEC -> codepoint 0 '0'; Interval codepoint 2 '10'
# (S = 16 at tau/8, so 168*16); long '1'; additional midamble present '1'; D2R TBS 20 = codepoint 19 '0010011'.
SCHED_DATA_BITS = "100" "010" "1" "0" "10" "1" "1" "0010011"
# CBRA paging (18 bits): X = 2 '1'; tau/4 '011'; bitmap {1,2,16} '11001000'; Block 1 '0'; no FEC '1';
# Interval codepoint 0 '00'; short '0'; additional midamble absent '0'.
SCHED_CBRA_BITS = "1" "011" "11001000" "0" "1" "00" "0" "0"
# CFA paging (24 bits): 2*tau '000'; bitmap {128} '00000001'; Block 2 '1'; FEC '0'; Interval cp 1 '01';
# short '0'; absent '0'; D2R TBS 125 = codepoint 124 '1111100'.
SCHED_CFA_BITS = "000" "00000001" "1" "0" "01" "0" "0" "1111100"
# Random ID Response (24 bits): tau/2 '010'; bitmap {1,2,4,8,16,32} '11111100'; Block 1 '0'; FEC '0';
# Interval cp 3 '11'; long '1'; present '1'; D2R TBS 33 = codepoint 32 '0100000'.
SCHED_RIDR_BITS = "010" "11111100" "0" "0" "11" "1" "1" "0100000"


def test_sched_hand_bits():
    for variant, s, n in ((SCHED_R2D_DATA, SCHED_DATA_BITS, 19), (SCHED_PAGING_CBRA, SCHED_CBRA_BITS, 18),
                          (SCHED_PAGING_CFA, SCHED_CFA_BITS, 24), (SCHED_RANDOM_ID_RESPONSE, SCHED_RIDR_BITS, 24)):
        assert len(s) == n
        parsed = mac.sched_parse(b(s), variant)
        assert mac.sched_pack(parsed) == b(s)
    d = mac.sched_parse(b(SCHED_DATA_BITS), SCHED_R2D_DATA)
    assert sv(d) == (Fraction(1, 8), (4,), None, 2, True, 168 * 16, 31, True, 20)
    c = mac.sched_parse(b(SCHED_CBRA_BITS), SCHED_PAGING_CBRA)
    assert sv(c) == (Fraction(1, 4), (1, 2, 16), 2, 1, False, 48 * 8, 7, False, None)


# The R2D TBS field holds the TBS in bytes minus one (38.391 6.1.1: the first codepoint is the first
# value, 1 byte), as the D2R TBS field does.
def test_hand_paging_cbra():
    s = ("001" "0000101" "0" "1" "100101" "0" "0101" + SCHED_CBRA_BITS + "1" + "000000")
    # type 001, TBS 6, SPPI 0, AT 1, TID 37, PIPI 0, 2^5 access occasions, sched, K = 4, 6 fill bits
    assert len(s) == 48
    m = mac.parse_r2d(b(s))
    sch = sv(mac.sched_parse(b(SCHED_CBRA_BITS), SCHED_PAGING_CBRA))
    assert view(m) == (PAGING_CBRA, 6, sch, 37, 5, 1, None, None)
    assert mac.num_access_occasions(m.num_access_occasions_code) == 32 and mac.k_value(m.k_bit) == 4
    assert mac.pack_r2d(m) == b(s)
    # with a Paging ID: PIPI 1, length 20 '0000010100' (10 bits), 20 ID bits; 42 + 30 = 72 bits, TBS 9, no fill
    pid = "10110011100011110000"
    s = "001" "0001000" "0" "1" "100101" "1" "0000010100" + pid + "0101" + SCHED_CBRA_BITS + "1"
    assert len(s) == 72
    m = mac.parse_r2d(b(s))
    assert pid_of(m) == tuple(b(pid)) and sec_of(m) is None and mac.pack_r2d(m) == b(s)
    # SPPI 1: the 128-bit Security Parameter follows the SPPI bit; 170 bits, TBS 22, 6 fill bits
    sec = "".join("1" if i % 3 == 0 else "0" for i in range(128))
    s = "001" "0010101" "1" + sec + "1" "100101" "0" "0101" + SCHED_CBRA_BITS + "1" + "000000"
    assert len(s) == 176
    m = mac.parse_r2d(b(s))
    assert view(m) == (PAGING_CBRA, 22, sch, 37, 5, 1, None, tuple(b(sec)))
    assert mac.pack_r2d(m) == b(s)


def test_hand_paging_cfa():
    pid = "101100111000"
    # type, TBS 8, SPPI 0, AT 0, length 12 on 10 bits, ID, sched: 58 bits + 6 fill
    s = "001" "0000111" "0" "0" "0000001100" + pid + SCHED_CFA_BITS + "000000"
    assert len(s) == 64
    m = mac.parse_r2d(b(s))
    assert pid_of(m) == tuple(b(pid)) and tuple(m.sched.shifts) == (128,) and m.sched.d2r_tbs == 125
    assert sec_of(m) is None
    assert mac.pack_r2d(m) == b(s)
    # SPPI 1 with a 16-bit ID: 190 bits, TBS 24, 2 fill bits
    pid = "1011001110001111"
    sec = "".join("0" if i % 2 else "1" for i in range(128))
    s = "001" "0010111" "1" + sec + "0" "0000010000" + pid + SCHED_CFA_BITS + "00"
    assert len(s) == 192
    m = mac.parse_r2d(b(s))
    assert pid_of(m) == tuple(b(pid)) and sec_of(m) == tuple(b(sec)) and mac.pack_r2d(m) == b(s)


def test_hand_access_trigger():
    assert mac.parse_r2d(b("010")).kind == ACCESS_TRIGGER


def test_hand_random_id_response():
    s = ("011" "0001000" + SCHED_RIDR_BITS + "1"
         "101" "1010101111001101" "1" "0001001000110100"      # FI 5 (R_SFS 32), echoed 0xABCD, AIPI, AS ID 0x1234
         "0")                                                  # fill bit
    assert len(s) == 72
    m = mac.parse_r2d(b(s))
    assert view(m)[3] == ((0xABCD, 0x1234, 5),) and mac.freq_index_r_sfs(5) == 32
    assert mac.pack_r2d(m) == b(s)
    # two entries, FIPI 0: first without AS ID (17 bits), second with (33 bits): 35 + 17 + 33 = 85 bits -> 88, TBS 11
    s = ("011" "0001010" + SCHED_RIDR_BITS + "0" "0000000000000001" "0" "0000000000000010" "1" "0000000000000011" + "000")
    assert len(s) == 88
    m = mac.parse_r2d(b(s))
    assert view(m)[3] == ((1, None, None), (2, 3, None))     # no Frequency Index in any entry: FIPI 0
    assert mac.pack_r2d(m) == b(s)


def test_hand_r2d_upper_layer_data():
    s = "100" "0000111" "1011111011101111" + SCHED_DATA_BITS + "1" "00" "1010010100001111"
    assert len(s) == 64
    m = mac.parse_r2d(b(s))
    assert m.as_id == 0xBEEF and bytes(m.sdu[:m.sdu_len]) == b"\xa5\x0f" and m.has_sdu == 1 and m.sched.d2r_tbs == 20
    assert mac.pack_r2d(m) == b(s)
    s = "100" "0000110" "0000000000000001" + SCHED_DATA_BITS + "0" "000" "1100100"
    assert len(s) == 56
    m = mac.parse_r2d(b(s))
    assert m.as_id == 1 and m.received_data_size == 100 and m.has_sdu == 0 and mac.pack_r2d(m) == b(s)


def test_hand_nack():
    s = "101" "0000101" "000000" "0000000011111111" "1000000000000001"
    m = mac.parse_r2d(b(s))
    assert view(m) == (NACK_FEEDBACK, 6, (0x00FF, 0x8001))
    assert mac.pack_r2d(m) == b(s)


def test_hand_d2r():
    assert mac.pack_d2r(mac.access_random_id(0x8001)) == b("1000000000000001")
    assert mac.parse_d2r(b("1000000000000001"), D2R_ACCESS_RANDOM_ID).random_id == 0x8001
    # type 00, R 000000, MDI 1, SDU length 3, SDU 'abc', two padding bytes
    s = "00" "000000" "1" "0000011" "01100001" "01100010" "01100011" "00000000" "00000000"
    m = mac.parse_d2r(b(s), D2R_UPPER_LAYER_DATA, d2r_tbs=7)
    assert d2r_view(m) == (b"abc", 1, 2)
    assert mac.pack_d2r(m) == b(s)
    assert d2r_view(mac.d2r_for_tbs(b"abc", 7, mdi=1)) == d2r_view(m)
    # no data: SDU length 0, MDI 0
    assert mac.pack_d2r(mac.d2r_data(b"")) == b("00000000 00000000")


# ----------------------------------------------------------------------------------------------
# Scheduling Info validity
# ----------------------------------------------------------------------------------------------

MAX_SFS = {Fraction(2): 128, Fraction(1): 64, Fraction(1, 2): 32, Fraction(1, 4): 16, Fraction(1, 8): 8,
           Fraction(1, 16): 4, Fraction(1, 32): 2, Fraction(1, 96): 1}     # Table 6.2.1.6-1
SCALE = dict(zip(BIT_DURATIONS, (1, 2, 4, 8, 16, 32, 64, 192)))             # Interval Bits


@pytest.mark.parametrize("t", BIT_DURATIONS)
def test_r_sfs_gating(t):
    top = MAX_SFS[t]
    ok = tuple(r for r in R_SFS_VALUES if r <= top)
    s = sched(SCHED_PAGING_CBRA, t_bit_tau=t, r_sfs=ok, i_bit=48 * SCALE[t])
    assert tuple(mac.sched_parse(mac.sched_pack(s), SCHED_PAGING_CBRA).shifts) == ok
    for bad in (r for r in R_SFS_VALUES if r > top):
        with pytest.raises(ValueError):
            sched(SCHED_PAGING_CBRA, t_bit_tau=t, r_sfs=ok + (bad,), i_bit=48 * SCALE[t])
        # and a hand-built bitmap with the forbidden bit set is rejected on parse
        bitmap = "".join("1" if r == bad else "0" for r in R_SFS_VALUES)
        bits = "1" + format(BIT_DURATIONS.index(t), "03b") + bitmap + "0" "1" "00" "0" "0"
        with pytest.raises(ValueError):
            mac.sched_parse(b(bits), SCHED_PAGING_CBRA)
    # the Unicast value is limited by the Bit Duration as the Broadcast ones (CR 0009)
    u = sched(SCHED_R2D_DATA, t_bit_tau=t, r_sfs=(top,), i_bit=48 * SCALE[t])
    assert tuple(mac.sched_parse(mac.sched_pack(u), SCHED_R2D_DATA).shifts) == (top,)
    if top < 128:
        with pytest.raises(ValueError):
            sched(SCHED_R2D_DATA, t_bit_tau=t, r_sfs=(top * 2,), i_bit=48 * SCALE[t])
        # and so is a hand-built 19-bit field with the codepoint of top * 2
        cp = R_SFS_VALUES.index(top * 2)
        bits = format(BIT_DURATIONS.index(t), "03b") + format(cp, "03b") + "0" "0" "00" "0" "0" "0000001"
        with pytest.raises(ValueError):
            mac.sched_parse(b(bits), SCHED_R2D_DATA)


@pytest.mark.parametrize("t", BIT_DURATIONS)
def test_i_bit_scale(t):
    s = SCALE[t]
    assert i_bit_values(t) == (48 * s, 96 * s, 168 * s, 240 * s)
    for cp, base in enumerate((48, 96, 168, 240)):
        x = sched(SCHED_PAGING_CFA, t_bit_tau=t, r_sfs=(1,), i_bit=base * s)
        bits = mac.sched_pack(x)
        # the scale S follows from T_bit: the packed codepoint is the index in the base list,
        # and parsing it gives back base * S
        assert bits[3 + 8 + 1 + 1: 3 + 8 + 1 + 1 + 2] == b(format(cp, "02b"))
        assert mac.sched_parse(bits, SCHED_PAGING_CFA).i_bit == base * s
    with pytest.raises(ValueError):
        sched(SCHED_PAGING_CFA, t_bit_tau=t, r_sfs=(1,), i_bit=48 * s + 1)
    if s > 1:       # the unscaled value is only valid at 2*tau
        with pytest.raises(ValueError):
            sched(SCHED_PAGING_CFA, t_bit_tau=t, r_sfs=(1,), i_bit=48)


def test_sched_other_validity():
    with pytest.raises(ValueError):
        sched(SCHED_PAGING_CBRA, r_sfs=())                      # empty bitmap
    with pytest.raises(ValueError):
        sched(SCHED_PAGING_CFA, r_sfs=(1, 2))                   # exactly one R_SFS
    with pytest.raises(ValueError):
        sched(SCHED_R2D_DATA, r_sfs=(1, 2))
    with pytest.raises(ValueError):
        sched(SCHED_PAGING_CFA, d2r_tbs=0)                      # D2R TBS is 1..125
    with pytest.raises(ValueError):
        sched(SCHED_PAGING_CFA, d2r_tbs=126)
    with pytest.raises(ValueError):
        sched(SCHED_PAGING_CBRA, d2r_tbs=5)                     # absent in CBRA paging
    with pytest.raises(ValueError):
        sched(SCHED_PAGING_CFA, x=1)                            # Time Resource Indication is CBRA only
    with pytest.raises(ValueError):
        sched(SCHED_PAGING_CBRA, x=3)
    with pytest.raises(ValueError):
        sched(SCHED_PAGING_CFA, t_bit_tau=Fraction(3))
    with pytest.raises(ValueError):
        sched(SCHED_PAGING_CFA, r_block=3)
    with pytest.raises(ValueError):
        sched(SCHED_PAGING_CFA, l_amble=15)


@pytest.mark.parametrize("code", (125, 126, 127))
def test_unused_tbs_codepoints_rejected_on_parse(code):
    bits = b(SCHED_DATA_BITS)[:-7] + b(format(code, "07b"))
    with pytest.raises(ValueError):
        mac.sched_parse(bits, SCHED_R2D_DATA)


def test_to_d2r_params():
    s = mac.sched_parse(b(SCHED_RIDR_BITS), SCHED_RANDOM_ID_RESPONSE)
    p = mac.to_d2r_params(s, 8, "ook")
    assert p == D2RParams(t_bit_tau=Fraction(1, 2), r_sfs=8, r_block=1, r_code=True, l_amble=31,
                          i_bit=240 * 4, i_add=True, modulation="ook")
    assert s.d2r_tbs == 33
    with pytest.raises(ValueError):
        mac.to_d2r_params(s, 64)
    with pytest.raises(ValueError):
        mac.to_d2r_params(s, 128)
    c = mac.to_d2r_params(mac.sched_parse(b(SCHED_CBRA_BITS), SCHED_PAGING_CBRA), 16)
    assert c.t_bit_tau == Fraction(1, 4) and c.r_code is False and c.l_amble == 7 and c.i_bit == 48 * 8


# ----------------------------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------------------------

def test_pack_range_errors():
    cb = sched(SCHED_PAGING_CBRA)
    cf = sched(SCHED_PAGING_CFA)
    pack = mac.pack_r2d
    with pytest.raises(ValueError):
        pack(mac.paging_cbra(64, cb))                                  # Transaction ID is 6 bits
    with pytest.raises(ValueError):
        pack(mac.paging_cbra(0, cb, n_occasions_code=16))              # Number of Access Occasions is 4 bits
    with pytest.raises(ValueError):
        pack(mac.paging_cfa([0] * 955, cf))                            # 125 bytes hold at most 954 ID bits
    with pytest.raises(ValueError):
        pack(mac.paging_cfa([0, 2], cf))                               # a Paging ID is bits
    with pytest.raises(ValueError):
        mac.paging_cfa([0] * 1024, cf)                                 # Paging ID Length is 10 bits
    with pytest.raises(ValueError):
        mac.paging_cfa([0], cf, security_param=[0] * 127)              # a Security Parameter is 128 bits
    with pytest.raises(ValueError):
        mac.paging_cbra(0, cb, security_param=[0] * 127 + [2])         # of bits
    bad = mac.paging_cfa([0], cf)
    bad.has_security_param = 2                                         # SPPI is a flag
    with pytest.raises(ValueError):
        pack(bad)
    with pytest.raises(ValueError):
        pack(mac.paging_cfa([0], cb))                                  # Scheduling Info of the wrong variant
    with pytest.raises(ValueError):
        pack(mac.paging_cfa([0] * 8, cf, r2d_tbs=1))                   # TBS too small for the message
    with pytest.raises(ValueError):
        pack(mac.paging_cfa([0] * 8, cf, r2d_tbs=126))                 # TBS above 125
    rs = sched(SCHED_RANDOM_ID_RESPONSE)
    with pytest.raises(ValueError):
        pack(mac.random_id_response([(1 << 16, None, None)], rs))      # Echoed Random ID is 16 bits
    with pytest.raises(ValueError):
        mac.random_id_response([], rs)                                 # 1 to 8 entries
    with pytest.raises(ValueError):
        mac.random_id_response([(1, None, None)] * 9, rs)
    with pytest.raises(ValueError):
        pack(mac.random_id_response([(1, None, 2), (2, None, None)], rs))   # FIPI is for all entries or none
    with pytest.raises(ValueError):
        pack(mac.random_id_response([(1, None, 8)], rs))               # Frequency Index is 3 bits
    ds = sched(SCHED_R2D_DATA)
    with pytest.raises(ValueError):
        pack(mac.r2d_data(1, ds, received=126))                        # Received Data Size is 0..125
    with pytest.raises(ValueError):
        mac.r2d_data(1, ds, received=127)
    with pytest.raises(ValueError):
        pack(mac.r2d_data(-1, ds, received=0))                         # AS ID is 16 bits
    with pytest.raises(ValueError):
        pack(mac.r2d_data(1, ds, sdu=bytes(120)))                      # 126 bytes
    pack(mac.r2d_data(1, ds, sdu=bytes(119)))                          # 125 bytes is the limit
    with pytest.raises(ValueError):
        mac.nack([])                                                   # at least one AS ID
    with pytest.raises(ValueError):
        pack(mac.nack([1] * 62))                                       # 2 + 124 = 126 bytes
    with pytest.raises(ValueError):
        mac.pack_d2r(mac.access_random_id(1 << 16))
    with pytest.raises(ValueError):
        mac.pack_d2r(mac.d2r_data(bytes(128)))                         # SDU Length is 7 bits
    with pytest.raises(ValueError):
        mac.pack_d2r(mac.d2r_data(bytes(124)))                         # 126 bytes > 125
    with pytest.raises(ValueError):
        mac.pack_d2r(mac.d2r_data(b"", mdi=2))
    with pytest.raises(ValueError):
        mac.d2r_for_tbs(b"abc", 4)                                     # does not fit


def test_parse_errors():
    good = mac.pack_r2d(mac.paging_cfa([1] * 10, sched(SCHED_PAGING_CFA)))
    assert mac.parse_r2d(good) is not None
    # parse_r2d gives None for bits that are not an R2D message
    assert mac.parse_r2d(b("000") + good[3:]) is None          # reserved types
    assert mac.parse_r2d(b("110") + good[3:]) is None
    assert mac.parse_r2d(b("111") + good[3:]) is None
    assert mac.parse_r2d([]) is None                           # too short
    assert mac.parse_r2d(good + [0]) is None                   # not byte aligned
    assert mac.parse_r2d(good + [0] * 8) is None               # TBS field does not match the length
    assert mac.parse_r2d(good[:-8]) is None
    assert mac.parse_r2d([0, 1, 0, 0, 0, 0, 0, 0]) is None     # an Access Trigger is exactly 3 bits
    assert mac.parse_r2d([0, 1, 2]) is None                    # only 0 and 1
    assert mac.parse_r2d(b("001 0000000") + [0] * 6) is None   # TBS 0
    # Random ID Response with 8..16 stray bits after the last entry
    ridr = mac.pack_r2d(mac.random_id_response([(1, None, None)], sched(SCHED_RANDOM_ID_RESPONSE)))   # 56 bits
    assert mac.parse_r2d(b("011 0001000") + ridr[10:] + [0] * 8) is None                              # TBS 8
    # paging whose Paging ID Length runs past the end
    assert mac.parse_r2d(b("001 0000011 0 0") + b("1111111111") + [0] * 2) is None
    # SPPI 1 whose 128 bits run past the end
    assert mac.parse_r2d(b("001 0000011 1 0") + [0] * 12) is None
    # reserved codepoint of D2R TBS inside a message
    cfa = mac.pack_r2d(mac.paging_cfa([1] * 12, sched(SCHED_PAGING_CFA)))          # 58 bits + 6 fill
    cfa[51:58] = b("1111110")                                                      # D2R TBS codepoint 126
    assert mac.parse_r2d(cfa) is None
    # the other parsers raise ValueError
    with pytest.raises(ValueError):
        mac.parse_d2r([0] * 15, D2R_ACCESS_RANDOM_ID)          # exactly 16 bits
    with pytest.raises(ValueError):
        mac.parse_d2r([0] * 16, 7)                             # not a D2R message kind
    with pytest.raises(ValueError):
        mac.parse_d2r(b("01000000 00000000"), D2R_UPPER_LAYER_DATA)    # reserved type
    with pytest.raises(ValueError):
        mac.parse_d2r([0] * 12, D2R_UPPER_LAYER_DATA)          # not a whole number of bytes
    with pytest.raises(ValueError):
        mac.parse_d2r(b("00000000 0 0000100 00000000"), D2R_UPPER_LAYER_DATA)   # SDU Length runs past the end
    with pytest.raises(ValueError):
        mac.parse_d2r(b("00000000 00000000"), D2R_UPPER_LAYER_DATA, d2r_tbs=3)  # length differs from D2R TBS
    with pytest.raises(ValueError):
        mac.sched_parse([0] * 17, SCHED_PAGING_CBRA)
    with pytest.raises(ValueError):
        mac.sched_parse([0] * 18, 4)                           # no such variant


def test_reserved_bits_and_fill_are_ignored_on_parse():
    m = mac.nack([0x1234])
    bits = finish(m)
    bits[10:16] = [1] * 6
    assert view(mac.parse_r2d(bits)) == view(m)
    p = mac.paging_cfa([1, 0, 1], sched(SCHED_PAGING_CFA))
    bits = finish(p)
    bits[-1] = 1                                   # last fill bit
    assert view(mac.parse_r2d(bits)) == view(p)
    bits = mac.pack_d2r(mac.d2r_data(b"\x01", padding_bytes=1))
    bits[2:8] = [1] * 6
    bits[-8:] = [1] * 8
    assert d2r_view(mac.parse_d2r(bits, D2R_UPPER_LAYER_DATA)) == (b"\x01", 0, 1)


def test_paging_with_longer_tbs_keeps_fill():
    p = mac.paging_cfa([1] * 5, sched(SCHED_PAGING_CFA), r2d_tbs=20)
    bits = mac.pack_r2d(p)
    assert len(bits) == 160 and view(mac.parse_r2d(bits)) == view(p)


# ----------------------------------------------------------------------------------------------
# V19.4.0: Security Parameter, 10-bit Paging ID Length, Received Data Size 0..125, Fill Bits
# ----------------------------------------------------------------------------------------------

def test_security_parameter_follows_the_sppi_bit():
    sec = [int(i % 3 == 0) for i in range(128)]
    m = mac.paging_cbra(1, sched(SCHED_PAGING_CBRA), security_param=sec)
    bits = mac.pack_r2d(m)
    assert bits[10] == 1 and bits[11:139] == sec          # after type (3) and TBS (7)
    assert mac.pack_r2d(mac.paging_cbra(1, sched(SCHED_PAGING_CBRA)))[10] == 0     # SPPI 0 carries no bits
    for p in (m, mac.paging_cfa([1] * 7, sched(SCHED_PAGING_CFA), security_param=sec)):
        parsed = mac.parse_r2d(mac.pack_r2d(p))
        assert parsed.has_security_param == 1 and list(parsed.security_param) == sec


def test_paging_id_length_has_ten_bits():
    cf = sched(SCHED_PAGING_CFA)
    # 125 bytes hold 3 + 7 + 1 + 1 + 10 + L + 24 <= 1000 bits: L <= 954 without a Security Parameter
    for n in (0, 255, 256, 300, 954):
        m = mac.paging_cfa([n % 2] * n, cf)
        bits = mac.pack_r2d(m)
        assert bits[12:22] == b(format(n, "010b"))
        assert pid_of(mac.parse_r2d(bits)) == pid_of(m)
    assert len(mac.pack_r2d(mac.paging_cfa([1] * 954, cf))) == 1000
    with pytest.raises(ValueError):
        mac.pack_r2d(mac.paging_cfa([1] * 955, cf))
    sec = [1] * 128
    assert len(mac.pack_r2d(mac.paging_cfa([1] * 826, cf, security_param=sec))) == 1000      # 174 + 826
    with pytest.raises(ValueError):
        mac.pack_r2d(mac.paging_cfa([1] * 827, cf, security_param=sec))
    cb = sched(SCHED_PAGING_CBRA)
    assert len(mac.pack_r2d(mac.paging_cbra(1, cb, paging_id=[1] * 820, security_param=sec))) == 1000
    with pytest.raises(ValueError):
        mac.pack_r2d(mac.paging_cbra(1, cb, paging_id=[1] * 821, security_param=sec))


def test_paging_fill_bits_are_ignored_on_parse():
    sec = [1] * 128
    for p in (mac.paging_cbra(3, sched(SCHED_PAGING_CBRA), security_param=sec),
              mac.paging_cfa([1, 0, 1], sched(SCHED_PAGING_CFA)),
              mac.paging_cbra(3, sched(SCHED_PAGING_CBRA), paging_id=[1] * 5)):
        p.r2d_tbs = 0
        body = mac.r2d_body_bits(p)
        p.r2d_tbs = (body + 7) // 8 + 2                          # two Fill Bytes besides the Fill Bits
        bits = mac.pack_r2d(p)
        assert set(bits[body:]) == {0}
        ones = bits[:body] + [1] * (len(bits) - body)            # Fill Bits of any value (CR 0016)
        parsed = mac.parse_r2d(ones)
        assert parsed is not None and view(parsed) == view(p)
        assert mac.pack_r2d(parsed) == bits


def test_received_data_size_is_0_to_125():
    ds = sched(SCHED_R2D_DATA)
    for rds in (0, 1, 125):
        m = mac.r2d_data(7, ds, received=rds)
        assert mac.parse_r2d(mac.pack_r2d(m)).received_data_size == rds
    for rds in (126, 127, 128, -1):
        with pytest.raises(ValueError):
            mac.r2d_data(7, ds, received=rds)
    ci0 = "100" "0000110" "0000000000000111" + SCHED_DATA_BITS + "0" "000"
    assert mac.parse_r2d(b(ci0 + "1111101")).received_data_size == 125
    assert mac.parse_r2d(b(ci0 + "1111110")) is None
    assert mac.parse_r2d(b(ci0 + "1111111")) is None
