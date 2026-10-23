# tests/test_mac_pdu.py
"""Tests of amber.mac.pdu (3GPP TS 38.391 V19.0.0 clause 6)."""

import random
from fractions import Fraction

import pytest

from amber.mac.pdu import (
    AccessRandomID, AccessTrigger, AIoTPagingCBRA, AIoTPagingCFA, D2RUpperLayerDataTransfer,
    NACKFeedback, R2DUpperLayerDataTransfer, RandomIDEntry, RandomIDResponse, SchedulingInfo,
    SCHED_PAGING_CBRA, SCHED_PAGING_CFA, SCHED_R2D_DATA, SCHED_RANDOM_ID_RESPONSE, SCHED_VARIANTS,
    SCHED_WIDTH, parse_d2r, parse_r2d, parse_sched_info, to_d2r_params)
from amber.phy.params import BIT_DURATIONS, R_SFS_VALUES, D2RParams, allowed_r_sfs, i_bit_values

N_ROUNDS = 300


def b(s: str):
    """A bit string such as '001 0110' (spaces ignored) as a list of ints."""
    return [int(c) for c in s if c in "01"]


def rand_bits(rng, n):
    return [rng.randint(0, 1) for _ in range(n)]


def rand_sched(rng, variant):
    t = rng.choice(BIT_DURATIONS)
    ok = allowed_r_sfs(t)
    if variant in (SCHED_PAGING_CFA, SCHED_R2D_DATA):
        sfs = (rng.choice(ok),)
    else:
        sfs = tuple(r for r in ok if rng.random() < 0.5) or (rng.choice(ok),)
    return SchedulingInfo(
        variant=variant, t_bit_tau=t, r_sfs=sfs, r_block=rng.choice((1, 2)), r_code=rng.random() < .5,
        i_bit=rng.choice(i_bit_values(t)), l_amble=rng.choice((7, 31)), i_add=rng.random() < .5,
        d2r_tbs=None if variant == SCHED_PAGING_CBRA else rng.randint(1, 125),
        x=rng.choice((1, 2)) if variant == SCHED_PAGING_CBRA else None)


def rand_cbra(rng):
    pid = rand_bits(rng, rng.randint(0, 255)) if rng.random() < .6 else None
    msg = AIoTPagingCBRA(transaction_id=rng.randrange(64), sched=rand_sched(rng, SCHED_PAGING_CBRA), paging_id=pid,
                         num_access_occasions_code=rng.randrange(16), k_bit=rng.randint(0, 1))
    if rng.random() < .3:          # extra zero Fill Bits ("future extensions") are allowed in paging
        msg = AIoTPagingCBRA(**{**msg.__dict__, "r2d_tbs": min(125, msg.r2d_tbs + rng.randint(0, 3))})
    return msg


def rand_cfa(rng):
    msg = AIoTPagingCFA(paging_id=rand_bits(rng, rng.randint(0, 255)), sched=rand_sched(rng, SCHED_PAGING_CFA))
    if rng.random() < .3:
        msg = AIoTPagingCFA(**{**msg.__dict__, "r2d_tbs": min(125, msg.r2d_tbs + rng.randint(0, 3))})
    return msg


def rand_ridr(rng):
    fipi = rng.random() < .5
    ents = [RandomIDEntry(echoed_random_id=rng.randrange(1 << 16),
                          assigned_as_id=rng.randrange(1 << 16) if rng.random() < .5 else None,
                          freq_index=rng.randrange(8) if fipi else None)
            for _ in range(rng.randint(1, 8))]
    return RandomIDResponse(sched=rand_sched(rng, SCHED_RANDOM_ID_RESPONSE), entries=ents)


def rand_r2d_data(rng):
    kw = dict(as_id=rng.randrange(1 << 16), sched=rand_sched(rng, SCHED_R2D_DATA))
    if rng.random() < .5:
        return R2DUpperLayerDataTransfer(data_sdu=bytes(rng.randrange(256) for _ in range(rng.randint(0, 119))), **kw)
    return R2DUpperLayerDataTransfer(received_data_size=rng.randrange(128), **kw)


def rand_nack(rng):
    return NACKFeedback(as_ids=[rng.randrange(1 << 16) for _ in range(rng.randint(1, 61))])


R2D_GENERATORS = [rand_cbra, rand_cfa, rand_ridr, rand_r2d_data, rand_nack]


# ----------------------------------------------------------------------------------------------
# Round trips
# ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("gen", R2D_GENERATORS, ids=lambda g: g.__name__)
def test_r2d_round_trip(gen):
    rng = random.Random(0xA10 + len(gen.__name__))
    for _ in range(N_ROUNDS):
        msg = gen(rng)
        bits = msg.pack()
        assert len(bits) == 8 * msg.r2d_tbs and set(bits) <= {0, 1}
        assert parse_r2d(bits) == msg
        assert parse_r2d(bits).pack() == bits


def test_access_trigger_is_three_bits():
    assert AccessTrigger().pack() == [0, 1, 0]
    assert parse_r2d([0, 1, 0]) == AccessTrigger()


@pytest.mark.parametrize("variant", SCHED_VARIANTS)
def test_sched_round_trip(variant):
    rng = random.Random(7)
    for _ in range(N_ROUNDS):
        s = rand_sched(rng, variant)
        bits = s.pack()
        assert len(bits) == SCHED_WIDTH[variant]
        assert parse_sched_info(bits, variant) == s


def test_d2r_round_trip():
    rng = random.Random(11)
    for _ in range(N_ROUNDS):
        m1 = AccessRandomID(rng.randrange(1 << 16))
        assert parse_d2r(m1.pack(), "access_random_id") == m1
        sdu = bytes(rng.randrange(256) for _ in range(rng.randint(0, 100)))
        m2 = D2RUpperLayerDataTransfer(sdu=sdu, mdi=rng.randint(0, 1), padding_bytes=rng.randint(0, 120 - len(sdu)))
        bits = m2.pack()
        assert len(bits) == 8 * m2.length_bytes
        assert parse_d2r(bits, "upper_layer_data") == m2
        assert parse_d2r(bits, "upper_layer_data", d2r_tbs=m2.length_bytes) == m2


# ----------------------------------------------------------------------------------------------
# Exact lengths, worked out from the spec figures
# ----------------------------------------------------------------------------------------------

def test_sched_widths_match_table_6_2_1_6_1():
    # Child field lengths: TRI 1, Bit Duration 3, Freq Broadcast 8 / Unicast 3, Block 1, Coding 1,
    # Interval 2, Sequence 1, Additional Midamble 1, D2R TBS 7.
    assert SCHED_WIDTH[SCHED_PAGING_CBRA] == 1 + 3 + 8 + 1 + 1 + 2 + 1 + 1 == 18      # no D2R TBS
    assert SCHED_WIDTH[SCHED_PAGING_CFA] == 3 + 8 + 1 + 1 + 2 + 1 + 1 + 7 == 24       # no TRI
    assert SCHED_WIDTH[SCHED_RANDOM_ID_RESPONSE] == 24
    assert SCHED_WIDTH[SCHED_R2D_DATA] == 3 + 3 + 1 + 1 + 2 + 1 + 1 + 7 == 19         # unicast, no TRI


def sched(variant, **kw):
    d = dict(t_bit_tau=Fraction(1, 8), r_sfs=(4,), i_bit=48 * 16)
    if variant != SCHED_PAGING_CBRA:
        d["d2r_tbs"] = 20
    else:
        d["x"] = 1
    d.update(kw)
    return SchedulingInfo(variant=variant, **d)


def test_hand_computed_lengths():
    # CBRA paging, PIPI = 0: 3 type + 7 TBS + 1 AT + 6 TID + 1 PIPI + 4 NAO + 18 sched + 1 K = 41 bits
    # -> 7 fill bits -> 48 bits, TBS 6.
    m = AIoTPagingCBRA(transaction_id=1, sched=sched(SCHED_PAGING_CBRA))
    assert (len(m.pack()), m.r2d_tbs) == (48, 6)
    # With a 20-bit Paging ID: 41 + 8 (length) + 20 = 69 bits -> 72, TBS 9.
    m = AIoTPagingCBRA(transaction_id=1, sched=sched(SCHED_PAGING_CBRA), paging_id=[1] * 20)
    assert (len(m.pack()), m.r2d_tbs) == (72, 9)
    # CFA paging, 32-bit Paging ID: 3 + 7 + 1 + 8 + 32 + 24 = 75 bits -> 80, TBS 10.
    m = AIoTPagingCFA(paging_id=[0] * 32, sched=sched(SCHED_PAGING_CFA))
    assert (len(m.pack()), m.r2d_tbs) == (80, 10)
    # Random ID Response, one entry without FI or AS ID: 3 + 7 + 24 + 1 FIPI + (16 + 1) = 52 -> 56, TBS 7.
    rs = sched(SCHED_RANDOM_ID_RESPONSE)
    m = RandomIDResponse(sched=rs, entries=[RandomIDEntry(5)])
    assert (len(m.pack()), m.r2d_tbs) == (56, 7)
    # One entry with FI and AS ID: 35 + (3 + 16 + 1 + 16) = 71 -> 72, TBS 9.
    m = RandomIDResponse(sched=rs, entries=[RandomIDEntry(5, 6, 1)])
    assert (len(m.pack()), m.r2d_tbs) == (72, 9)
    # Three such entries: 35 + 3 * 20 (FI, ID, AIPI, no AS ID) = 95 -> 96, TBS 12.
    m = RandomIDResponse(sched=rs, entries=[RandomIDEntry(i, None, 0) for i in range(3)])
    assert (len(m.pack()), m.r2d_tbs) == (96, 12)
    # Eight entries with everything: 35 + 8 * 36 = 323 -> 328, TBS 41.
    m = RandomIDResponse(sched=rs, entries=[RandomIDEntry(i, i, 0) for i in range(8)])
    assert (len(m.pack()), m.r2d_tbs) == (328, 41)
    # R2D data, CI = 1: 3 + 7 + 16 + 19 + 1 + 2 R = 48 bits + SDU; 5-byte SDU -> 88 bits, TBS 11.
    ds = sched(SCHED_R2D_DATA)
    m = R2DUpperLayerDataTransfer(as_id=1, sched=ds, data_sdu=b"12345")
    assert (len(m.pack()), m.r2d_tbs) == (88, 11)
    # CI = 0: 3 + 7 + 16 + 19 + 1 + 3 R + 7 Received Data Size = 56 bits, TBS 7.
    m = R2DUpperLayerDataTransfer(as_id=1, sched=ds, received_data_size=9)
    assert (len(m.pack()), m.r2d_tbs) == (56, 7)
    # NACK: 3 + 7 + 6 R = 16 bits + 16 per AS ID; 3 IDs -> 64 bits, TBS 8.
    m = NACKFeedback(as_ids=[1, 2, 3])
    assert (len(m.pack()), m.r2d_tbs) == (64, 8)
    # Access Trigger: 3 bits, not padded. Msg1: 16 bits. D2R data: 2 + 4 SDU + 3 padding = 9 bytes.
    assert len(AccessTrigger().pack()) == 3
    assert len(AccessRandomID(7).pack()) == 16
    assert len(D2RUpperLayerDataTransfer(sdu=b"abcd", padding_bytes=3).pack()) == 72


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
        parsed = parse_sched_info(b(s), variant)
        assert parsed.pack() == b(s)
    d = parse_sched_info(b(SCHED_DATA_BITS), SCHED_R2D_DATA)
    assert (d.t_bit_tau, d.r_sfs, d.r_block, d.r_code, d.i_bit, d.l_amble, d.i_add, d.d2r_tbs) == \
           (Fraction(1, 8), (4,), 2, True, 168 * 16, 31, True, 20)
    c = parse_sched_info(b(SCHED_CBRA_BITS), SCHED_PAGING_CBRA)
    assert (c.x, c.t_bit_tau, c.r_sfs, c.r_code, c.i_bit, c.d2r_tbs) == (2, Fraction(1, 4), (1, 2, 16), False, 48 * 8, None)


def test_hand_paging_cbra():
    s = ("001" "0000110" "1" "100101" "0" "0101" + SCHED_CBRA_BITS + "1" + "0000000")
    # type 001, TBS 6, AT 1, TID 37, PIPI 0, 2^5 access occasions, sched, K = 4, 7 fill bits
    assert len(s) == 48
    m = parse_r2d(b(s))
    assert m == AIoTPagingCBRA(transaction_id=37, sched=parse_sched_info(b(SCHED_CBRA_BITS), SCHED_PAGING_CBRA),
                               paging_id=None, num_access_occasions_code=5, k_bit=1)
    assert m.num_access_occasions == 32 and m.k == 4
    assert m.pack() == b(s)
    # with a Paging ID: PIPI 1, length 20 '00010100', 20 ID bits; 41 + 28 = 69 bits, TBS 9, 3 fill bits
    pid = "10110011100011110000"
    s = "001" "0001001" "1" "100101" "1" "00010100" + pid + "0101" + SCHED_CBRA_BITS + "1" + "000"
    m = parse_r2d(b(s))
    assert m.paging_id == tuple(b(pid)) and m.pack() == b(s)


def test_hand_paging_cfa():
    pid = "101100111000"
    s = "001" "0000111" "0" "00001100" + pid + SCHED_CFA_BITS + "0"       # 55 bits + 1 fill
    assert len(s) == 56
    m = parse_r2d(b(s))
    assert m.paging_id == tuple(b(pid)) and m.sched.r_sfs == (128,) and m.sched.d2r_tbs == 125
    assert m.pack() == b(s)


def test_hand_access_trigger():
    assert parse_r2d(b("010")) == AccessTrigger()


def test_hand_random_id_response():
    s = ("011" "0001001" + SCHED_RIDR_BITS + "1"
         "101" "1010101111001101" "1" "0001001000110100"      # FI 5 (R_SFS 32), echoed 0xABCD, AIPI, AS ID 0x1234
         "0")                                                  # fill bit
    assert len(s) == 72
    m = parse_r2d(b(s))
    assert m.fipi and m.entries == (RandomIDEntry(0xABCD, 0x1234, 5),) and m.entries[0].r_sfs == 32
    assert m.pack() == b(s)
    # two entries, FIPI 0: first without AS ID (17 bits), second with (33 bits): 35 + 17 + 33 = 85 bits -> 88, TBS 11
    s = ("011" "0001011" + SCHED_RIDR_BITS + "0" "0000000000000001" "0" "0000000000000010" "1" "0000000000000011" + "000")
    assert len(s) == 88
    m = parse_r2d(b(s))
    assert m.entries == (RandomIDEntry(1), RandomIDEntry(2, 3)) and not m.fipi


def test_hand_r2d_upper_layer_data():
    s = "100" "0001000" "1011111011101111" + SCHED_DATA_BITS + "1" "00" "1010010100001111"
    assert len(s) == 64
    m = parse_r2d(b(s))
    assert m.as_id == 0xBEEF and m.data_sdu == b"\xa5\x0f" and m.ci == 1 and m.sched.d2r_tbs == 20
    assert m.pack() == b(s)
    s = "100" "0000111" "0000000000000001" + SCHED_DATA_BITS + "0" "000" "1100100"
    assert len(s) == 56
    m = parse_r2d(b(s))
    assert m.as_id == 1 and m.received_data_size == 100 and m.ci == 0 and m.pack() == b(s)


def test_hand_nack():
    s = "101" "0000110" "000000" "0000000011111111" "1000000000000001"
    m = parse_r2d(b(s))
    assert m == NACKFeedback(as_ids=[0x00FF, 0x8001]) and m.r2d_tbs == 6
    assert m.pack() == b(s)


def test_hand_d2r():
    assert AccessRandomID(0x8001).pack() == b("1000000000000001")
    assert parse_d2r(b("1000000000000001"), "access_random_id").random_id == 0x8001
    # type 00, R 000000, MDI 1, SDU length 3, SDU 'abc', two padding bytes
    s = "00" "000000" "1" "0000011" "01100001" "01100010" "01100011" "00000000" "00000000"
    m = parse_d2r(b(s), "upper_layer_data", d2r_tbs=7)
    assert m == D2RUpperLayerDataTransfer(sdu=b"abc", mdi=1, padding_bytes=2)
    assert m.pack() == b(s)
    assert D2RUpperLayerDataTransfer.for_tbs(b"abc", 7, mdi=1) == m
    # no data: SDU length 0, MDI 0
    assert D2RUpperLayerDataTransfer().pack() == b("00000000 00000000")


# ----------------------------------------------------------------------------------------------
# Scheduling Info validity
# ----------------------------------------------------------------------------------------------

MAX_SFS = {Fraction(2): 128, Fraction(1): 64, Fraction(1, 2): 32, Fraction(1, 4): 16, Fraction(1, 8): 8,
           Fraction(1, 16): 4, Fraction(1, 32): 2, Fraction(1, 96): 1}     # Table 6.2.1.6-1, p.22
SCALE = dict(zip(BIT_DURATIONS, (1, 2, 4, 8, 16, 32, 64, 192)))             # Interval Bits, p.23


@pytest.mark.parametrize("t", BIT_DURATIONS)
def test_r_sfs_gating(t):
    top = MAX_SFS[t]
    ok = tuple(r for r in R_SFS_VALUES if r <= top)
    s = sched(SCHED_PAGING_CBRA, t_bit_tau=t, r_sfs=ok, i_bit=48 * SCALE[t])
    assert parse_sched_info(s.pack(), SCHED_PAGING_CBRA).r_sfs == ok
    for bad in (r for r in R_SFS_VALUES if r > top):
        with pytest.raises(ValueError, match="R_SFS"):
            sched(SCHED_PAGING_CBRA, t_bit_tau=t, r_sfs=ok + (bad,), i_bit=48 * SCALE[t])
        # and a hand-built bitmap with the forbidden bit set is rejected on parse
        bitmap = "".join("1" if r == bad else "0" for r in R_SFS_VALUES)
        bits = "1" + format(BIT_DURATIONS.index(t), "03b") + bitmap + "0" "1" "00" "0" "0"
        with pytest.raises(ValueError):
            parse_sched_info(b(bits), SCHED_PAGING_CBRA)
    # the Unicast field has no bit-duration gating in Table 6.2.1.6-1: any of its 8 values is a valid message
    if top < 128:
        u = sched(SCHED_R2D_DATA, t_bit_tau=t, r_sfs=(top * 2,), i_bit=48 * SCALE[t])
        assert parse_sched_info(u.pack(), SCHED_R2D_DATA).r_sfs == (top * 2,)


@pytest.mark.parametrize("t", BIT_DURATIONS)
def test_i_bit_scale(t):
    s = SCALE[t]
    assert i_bit_values(t) == (48 * s, 96 * s, 168 * s, 240 * s)
    for cp, base in enumerate((48, 96, 168, 240)):
        x = sched(SCHED_PAGING_CFA, t_bit_tau=t, r_sfs=(1,), i_bit=base * s)
        assert x.scale == s and x.i_bit_codepoint == cp
        bits = x.pack()
        assert bits[3 + 8 + 1 + 1: 3 + 8 + 1 + 1 + 2] == b(format(cp, "02b"))
        assert parse_sched_info(bits, SCHED_PAGING_CFA).i_bit == base * s
    with pytest.raises(ValueError, match="I_bit"):
        sched(SCHED_PAGING_CFA, t_bit_tau=t, r_sfs=(1,), i_bit=48 * s + 1)
    if s > 1:       # the unscaled value is only valid at 2*tau
        with pytest.raises(ValueError, match="I_bit"):
            sched(SCHED_PAGING_CFA, t_bit_tau=t, r_sfs=(1,), i_bit=48)


def test_sched_other_validity():
    with pytest.raises(ValueError):
        sched(SCHED_PAGING_CBRA, r_sfs=())                      # empty bitmap
    with pytest.raises(ValueError, match="exactly one"):
        sched(SCHED_PAGING_CFA, r_sfs=(1, 2))
    with pytest.raises(ValueError, match="exactly one"):
        sched(SCHED_R2D_DATA, r_sfs=(1, 2))
    with pytest.raises(ValueError, match="TBS"):
        sched(SCHED_PAGING_CFA, d2r_tbs=0)
    with pytest.raises(ValueError, match="TBS"):
        sched(SCHED_PAGING_CFA, d2r_tbs=126)
    with pytest.raises(ValueError, match="absent"):
        sched(SCHED_PAGING_CBRA, d2r_tbs=5)
    with pytest.raises(ValueError, match="Time Resource"):
        sched(SCHED_PAGING_CFA, x=1)
    with pytest.raises(ValueError, match="Time Resource"):
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
    with pytest.raises(ValueError, match="not used"):
        parse_sched_info(bits, SCHED_R2D_DATA)


def test_to_d2r_params():
    s = parse_sched_info(b(SCHED_RIDR_BITS), SCHED_RANDOM_ID_RESPONSE)
    p = to_d2r_params(s, 8, "ook")
    assert p == D2RParams(t_bit_tau=Fraction(1, 2), r_sfs=8, r_block=1, r_code=True, l_amble=31,
                          i_bit=240 * 4, i_add=True, modulation="ook")
    assert s.d2r_tbs_bytes == 33
    with pytest.raises(ValueError, match="R_SFS"):
        to_d2r_params(s, 64)
    with pytest.raises(ValueError, match="R_SFS"):
        to_d2r_params(s, 128)
    c = to_d2r_params(parse_sched_info(b(SCHED_CBRA_BITS), SCHED_PAGING_CBRA), 16)
    assert c.t_bit_tau == Fraction(1, 4) and c.r_code is False and c.l_amble == 7 and c.i_bit == 48 * 8


# ----------------------------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------------------------

def test_pack_range_errors():
    cb = sched(SCHED_PAGING_CBRA)
    cf = sched(SCHED_PAGING_CFA)
    with pytest.raises(ValueError, match="Transaction ID"):
        AIoTPagingCBRA(transaction_id=64, sched=cb)
    with pytest.raises(ValueError, match="Number of Access Occasions"):
        AIoTPagingCBRA(transaction_id=0, sched=cb, num_access_occasions_code=16)
    with pytest.raises(ValueError, match="Paging ID"):
        AIoTPagingCFA(paging_id=[0] * 256, sched=cf)
    with pytest.raises(ValueError, match="Paging ID"):
        AIoTPagingCFA(paging_id=[0, 2], sched=cf)
    with pytest.raises(ValueError, match="variant"):
        AIoTPagingCFA(paging_id=[0], sched=cb)
    with pytest.raises(ValueError, match="TBS"):
        AIoTPagingCFA(paging_id=[0] * 8, sched=cf, r2d_tbs=1)
    with pytest.raises(ValueError, match="TBS"):
        AIoTPagingCFA(paging_id=[0] * 8, sched=cf, r2d_tbs=126)
    with pytest.raises(ValueError, match="Echoed"):
        RandomIDResponse(sched=sched(SCHED_RANDOM_ID_RESPONSE), entries=[RandomIDEntry(1 << 16)])
    with pytest.raises(ValueError, match="entries"):
        RandomIDResponse(sched=sched(SCHED_RANDOM_ID_RESPONSE), entries=[])
    with pytest.raises(ValueError, match="entries"):
        RandomIDResponse(sched=sched(SCHED_RANDOM_ID_RESPONSE), entries=[RandomIDEntry(1)] * 9)
    with pytest.raises(ValueError, match="FIPI"):
        RandomIDResponse(sched=sched(SCHED_RANDOM_ID_RESPONSE), entries=[RandomIDEntry(1, None, 2), RandomIDEntry(2)])
    with pytest.raises(ValueError, match="Frequency Index"):
        RandomIDResponse(sched=sched(SCHED_RANDOM_ID_RESPONSE), entries=[RandomIDEntry(1, None, 8)])
    ds = sched(SCHED_R2D_DATA)
    with pytest.raises(ValueError, match="exactly one"):
        R2DUpperLayerDataTransfer(as_id=1, sched=ds)
    with pytest.raises(ValueError, match="exactly one"):
        R2DUpperLayerDataTransfer(as_id=1, sched=ds, data_sdu=b"", received_data_size=0)
    with pytest.raises(ValueError, match="Received Data Size"):
        R2DUpperLayerDataTransfer(as_id=1, sched=ds, received_data_size=128)
    with pytest.raises(ValueError, match="AS ID"):
        R2DUpperLayerDataTransfer(as_id=-1, sched=ds, received_data_size=0)
    with pytest.raises(ValueError, match="TBS"):
        R2DUpperLayerDataTransfer(as_id=1, sched=ds, data_sdu=bytes(120))   # 126 bytes
    R2DUpperLayerDataTransfer(as_id=1, sched=ds, data_sdu=bytes(119))        # 125 bytes is the limit
    with pytest.raises(ValueError, match="at least one"):
        NACKFeedback(as_ids=[])
    with pytest.raises(ValueError, match="TBS"):
        NACKFeedback(as_ids=[1] * 62)                                         # 2 + 124 = 126 bytes
    with pytest.raises(ValueError, match="Random ID"):
        AccessRandomID(1 << 16).pack()
    with pytest.raises(ValueError, match="SDU Length"):
        D2RUpperLayerDataTransfer(sdu=bytes(128))
    with pytest.raises(ValueError):
        D2RUpperLayerDataTransfer(sdu=bytes(124))                             # 126 bytes > 125
    with pytest.raises(ValueError):
        D2RUpperLayerDataTransfer(sdu=b"", mdi=2)
    with pytest.raises(ValueError, match="does not fit"):
        D2RUpperLayerDataTransfer.for_tbs(b"abc", 4)


def test_parse_errors():
    good = AIoTPagingCFA(paging_id=[1] * 10, sched=sched(SCHED_PAGING_CFA)).pack()
    with pytest.raises(ValueError, match="reserved"):
        parse_r2d(b("000") + good[3:])
    with pytest.raises(ValueError, match="reserved"):
        parse_r2d(b("110") + good[3:])
    with pytest.raises(ValueError, match="reserved"):
        parse_r2d(b("111") + good[3:])
    with pytest.raises(ValueError, match="too short"):
        parse_r2d([])
    with pytest.raises(ValueError, match="byte aligned"):
        parse_r2d(good + [0])
    with pytest.raises(ValueError, match="TBS"):
        parse_r2d(good + [0] * 8)
    with pytest.raises(ValueError, match="TBS"):
        parse_r2d(good[:-8])
    with pytest.raises(ValueError, match="Access Trigger"):
        parse_r2d([0, 1, 0, 0, 0, 0, 0, 0])
    with pytest.raises(ValueError, match="only 0 and 1"):
        parse_r2d([0, 1, 2])
    with pytest.raises(ValueError, match="outside"):
        parse_r2d(b("001 0000000") + [0] * 6)                  # TBS 0
    # Random ID Response with 8..16 stray bits after the last entry
    ridr = RandomIDResponse(sched=sched(SCHED_RANDOM_ID_RESPONSE), entries=[RandomIDEntry(1)]).pack()   # 56 bits
    bad = b("011 0001000") + ridr[10:] + [0] * 8                                                        # TBS 8
    with pytest.raises(ValueError, match="Fill Bits"):
        parse_r2d(bad)
    # paging whose Paging ID Length runs past the end
    trunc = b("001 0000011 0") + b("11111111") + [0] * 5
    with pytest.raises(ValueError, match="truncated"):
        parse_r2d(trunc)
    # reserved codepoint of D2R TBS inside a message
    cfa = AIoTPagingCFA(paging_id=[1] * 12, sched=sched(SCHED_PAGING_CFA)).pack()   # 55 bits + 1 fill
    cfa[48:55] = b("1111110")                                                        # D2R TBS codepoint 126
    with pytest.raises(ValueError, match="not used"):
        parse_r2d(cfa)
    with pytest.raises(ValueError, match="exactly 16"):
        parse_d2r([0] * 15, "access_random_id")
    with pytest.raises(ValueError, match="kind"):
        parse_d2r([0] * 16, "msg1")
    with pytest.raises(ValueError, match="reserved"):
        parse_d2r(b("01000000 00000000"), "upper_layer_data")
    with pytest.raises(ValueError, match="whole number"):
        parse_d2r([0] * 12, "upper_layer_data")
    with pytest.raises(ValueError, match="SDU Length"):
        parse_d2r(b("00000000 0 0000100 00000000"), "upper_layer_data")
    with pytest.raises(ValueError, match="D2R TBS"):
        parse_d2r(b("00000000 00000000"), "upper_layer_data", d2r_tbs=3)
    with pytest.raises(ValueError):
        parse_sched_info([0] * 17, SCHED_PAGING_CBRA)
    with pytest.raises(ValueError, match="variant"):
        parse_sched_info([0] * 18, "nope")


def test_reserved_bits_and_fill_are_ignored_on_parse():
    m = NACKFeedback(as_ids=[0x1234])
    bits = m.pack()
    bits[10:16] = [1] * 6
    assert parse_r2d(bits) == m
    p = AIoTPagingCFA(paging_id=[1, 0, 1], sched=sched(SCHED_PAGING_CFA))
    bits = p.pack()
    bits[-1] = 1                                   # last fill bit
    assert parse_r2d(bits) == p
    d = D2RUpperLayerDataTransfer(sdu=b"\x01", padding_bytes=1)
    bits = d.pack()
    bits[2:8] = [1] * 6
    bits[-8:] = [1] * 8
    assert parse_d2r(bits, "upper_layer_data") == d


def test_paging_with_longer_tbs_keeps_fill():
    p = AIoTPagingCFA(paging_id=[1] * 5, sched=sched(SCHED_PAGING_CFA), r2d_tbs=20)
    bits = p.pack()
    assert len(bits) == 160 and parse_r2d(bits) == p
