"""Tests of the coding in Amber's PHY, OAI's C library (3GPP TS 38.291 V19.3.0, clause 8)."""

import numpy as np
import pytest

from amber.phy.aiot import (
    attach_crc,
    check_crc,
    combine_repetitions,
    crc,
    crc_length,
    repeat_block,
    tbcc_decode,
    tbcc_encode,
)

# generator polynomials as bit lists, highest degree first
G16 = [1] + [0] * 3 + [1] + [0] * 6 + [1] + [0] * 4 + [1]
G6 = [1, 1, 0, 0, 0, 0, 1]


def poly_remainder(bits, g):
    """Remainder of bits (as a polynomial, first bit highest) divided by g over GF(2)."""
    work = list(bits)
    n = len(g) - 1
    for i in range(len(work) - n):
        if work[i]:
            for j, gj in enumerate(g):
                work[i + j] ^= gj
    return work[-n:]


def rand_bits(rng, n):
    return [int(b) for b in rng.integers(0, 2, n)]


# ---------------------------------------------------------------- CRC

def test_generator_polynomials():
    assert len(G16) == 17 and G16[0] == 1 and G16[-1] == 1
    # D^16 + D^12 + D^5 + 1
    assert [16 - i for i, b in enumerate(G16) if b] == [16, 12, 5, 0]
    assert [6 - i for i, b in enumerate(G6) if b] == [6, 5, 0]


@pytest.mark.parametrize("L,g", [(16, G16), (6, G6)])
def test_codeword_remainder_is_zero(L, g):
    rng = np.random.default_rng(1)
    for A in range(1, 201):
        a = rand_bits(rng, A)
        word = a + crc(a, L)
        assert len(word) == A + L
        assert poly_remainder(word, g) == [0] * L


@pytest.mark.parametrize("L", [16, 6])
def test_crc_of_zero_payload_is_zero(L):
    for A in (1, 8, 24, 100):
        assert crc([0] * A, L) == [0] * L


def test_crc_hand_vectors():
    # payload "1": x^L mod g = g without its leading term
    assert crc([1], 16) == [0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]  # 0x1021
    assert crc([1], 6) == [1, 0, 0, 0, 0, 1]  # 0x21
    # payload "10": x^(L+1) mod g, i.e. the above shifted left by one
    assert crc([1, 0], 16) == [0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]  # 0x2042


def test_crc6_hand_vector_overflow():
    # payload "10" with CRC6: x^7 mod (x^6+x^5+1) = x*(x^5+1) = x^6+x = (x^5+1)+x = x^5+x+1
    assert crc([1, 0], 6) == [1, 0, 0, 0, 1, 1]


def test_crc_length_boundary():
    assert crc_length(1) == 6
    assert crc_length(24) == 6
    assert crc_length(25) == 16
    assert crc_length(100) == 16


def test_attach_and_check_crc_roundtrip():
    rng = np.random.default_rng(2)
    for A in list(range(1, 25)) + list(range(25, 120)):
        a = rand_bits(rng, A)
        word = attach_crc(a)
        assert len(word) == A + crc_length(A)
        if 31 <= len(word) <= 40:
            continue  # cannot occur (A = 25..., B >= 41; A <= 24, B <= 30)
        ok, payload = check_crc(word)
        assert ok and payload == a
        bad = list(word)
        bad[A // 2] ^= 1
        assert check_crc(bad)[0] is False


def test_check_crc_invalid_lengths():
    # A = 25..34 would give B = 41..50 and is fine; B in 31..40 is rejected
    for B in range(31, 41):
        with pytest.raises(ValueError):
            check_crc([0] * B)
    # boundaries: B = 30 uses CRC6 (A = 24), B = 41 uses CRC16 (A = 25)
    assert check_crc(attach_crc([1] * 24))[0] is True
    assert check_crc(attach_crc([1] * 25))[0] is True


# ------------------------------------------------------------------ TBCC

# 38.291 §8.2: rate 1/3, constraint length 7, generators 133, 171, 165 (octal)
GENERATORS = (0o133, 0o171, 0o165)


def tbcc_reference(c):
    """The tail-biting encoder written from 38.291 §8.2: the shift register starts with the last
    six input bits, and d^(i)_k is the parity of the generator's taps over c_k .. c_(k-6)."""
    K = len(c)
    out = []
    for k in range(K):
        window = [c[(k - j) % K] for j in range(7)]  # c_k, c_(k-1), ..., c_(k-6)
        for g in GENERATORS:
            taps = [(g >> (6 - j)) & 1 for j in range(7)]
            out.append(sum(t & b for t, b in zip(taps, window)) & 1)
    return out


@pytest.mark.parametrize("K", [6, 7, 8, 13, 16, 40, 97, 112, 336, 1000])
def test_tbcc_is_the_spec_code(K):
    rng = np.random.default_rng(1000 + K)
    for _ in range(3):
        c = rand_bits(rng, K)
        assert tbcc_encode(c) == tbcc_reference(c)


NOISELESS_K = [1, 2, 3, 5, 6, 7, 8, 13, 40, 112, 336, 1000]


@pytest.mark.parametrize("K", NOISELESS_K)
def test_tbcc_noiseless_roundtrip(K):
    rng = np.random.default_rng(K)
    for _ in range(3):
        c = rand_bits(rng, K)
        e = tbcc_encode(c)
        assert len(e) == 3 * K
        assert tbcc_decode(1.0 - 2.0 * np.array(e)) == c


@pytest.mark.parametrize("K", range(1, 41))
def test_tbcc_noiseless_roundtrip_short(K):
    rng = np.random.default_rng(100 + K)
    c = rand_bits(rng, K)
    assert tbcc_decode(1.0 - 2.0 * np.array(tbcc_encode(c))) == c


def test_tbcc_all_zero_and_all_one():
    for K in (1, 4, 9, 50):
        for bit in (0, 1):
            c = [bit] * K
            e = tbcc_encode(c)
            assert set(e) == {bit}  # all generators have odd weight
            assert tbcc_decode(1.0 - 2.0 * np.array(e)) == c


def test_tbcc_awgn_ber():
    rng = np.random.default_rng(7)
    K = 112
    rate = 1.0 / 3.0
    ebn0 = 10 ** (4.0 / 10)
    sigma = np.sqrt(1.0 / (2 * rate * ebn0))
    errors = 0
    trials = 200
    for _ in range(trials):
        c = rng.integers(0, 2, K)
        tx = 1.0 - 2.0 * np.array(tbcc_encode(c))
        rx = tx + sigma * rng.standard_normal(tx.size)
        errors += int(np.sum(np.array(tbcc_decode(rx)) != c))
    assert errors / (K * trials) < 1e-3


def test_tbcc_linear():
    rng = np.random.default_rng(3)
    for K in (1, 5, 6, 7, 20, 112):
        a = rng.integers(0, 2, K)
        b = rng.integers(0, 2, K)
        lhs = np.array(tbcc_encode(a ^ b))
        rhs = np.array(tbcc_encode(a)) ^ np.array(tbcc_encode(b))
        assert np.array_equal(lhs, rhs)


def test_tbcc_decode_speed():
    import time

    c = np.random.default_rng(0).integers(0, 2, 1000)
    soft = 1.0 - 2.0 * np.array(tbcc_encode(c))
    t0 = time.time()
    tbcc_decode(soft)
    assert time.time() - t0 < 1.0


# ------------------------------------------------------------ repetition

@pytest.mark.parametrize("r", [1, 2, 4, 8])
def test_repetition_roundtrip(r):
    rng = np.random.default_rng(r)
    b = rand_bits(rng, 37)
    rep = repeat_block(b, r)
    assert len(rep) == 37 * r
    assert rep[37:74 if r > 1 else 37] == (b if r > 1 else [])
    soft = 1.0 - 2.0 * np.array(rep)
    comb = combine_repetitions(soft, r)
    assert len(comb) == 37
    assert [int(x < 0) for x in comb] == b
    assert np.allclose(comb, r * (1.0 - 2.0 * np.array(b)))
