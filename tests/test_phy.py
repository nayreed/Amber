"""Tests of the Rel-19 PHY (amber.phy, amber.mac): spec conformance vectors, then R2D and D2R end to end."""

import os
import sys
from fractions import Fraction

import numpy as np
import pytest

from amber.phy import aiot
from amber.phy import params

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "conformance"))


def test_spec_conformance_vectors():
    """Every vector from the spec (tests/conformance/vectors) that this PHY and MAC answer must pass.
    An adapter method that is missing makes its vectors SKIP; one that raises fails the test."""
    from aiot_conformance.runner import run
    from amber_adapter import AmberAdapter

    totals, rows = run(AmberAdapter(), os.path.join(HERE, "conformance", "vectors"), None, False)
    assert totals["FAIL"] == 0, [r for r in rows if r[0] == "FAIL"]
    assert totals["PASS"] >= 340


# ----------------------------------------------------------------------------------------------
# R2D
# ----------------------------------------------------------------------------------------------

def _r2d_loop(tb, m, n_rb, noise_w, rng, lead=3000, tail=4000, amp=1.0):
    x = aiot.r2d_transmission(tb, m, n_rb)
    sig = np.concatenate([np.ones(lead), x, np.ones(tail)]) * amp
    rx = aiot.R2DReceiver(noise_w, rng)
    out = []
    for i in range(0, len(sig), params.SAMPLES_PER_MS):
        out += rx.receive(sig[i:i + params.SAMPLES_PER_MS])
    return out, lead + len(x)


@pytest.mark.parametrize("m,n_rb", [(2, 1), (6, 1), (12, 2), (24, 3), (24, 4)])
@pytest.mark.parametrize("a_bits", [3, 24, 96, 200])
def test_r2d_round_trip(m, n_rb, a_bits):
    rng = np.random.default_rng(a_bits * 100 + m)
    tb = [int(b) for b in rng.integers(0, 2, a_bits)]
    out, end = _r2d_loop(tb, m, n_rb, 1e-3, rng)
    assert [f.bits for f in out] == [tb]
    assert out[0].end_sample == end and out[0].m_chip_symb == m


def test_r2d_back_to_back_frames():
    rng = np.random.default_rng(5)
    tbs = [[int(b) for b in rng.integers(0, 2, 40)] for _ in range(3)]
    parts = [np.ones(2880)]
    for tb in tbs:
        parts += [aiot.r2d_transmission(tb, 6, 1), np.ones(2880)]
    sig = np.concatenate(parts)
    rx = aiot.R2DReceiver(1e-3, rng)
    out = []
    for i in range(0, len(sig), 5760):
        out += rx.receive(sig[i:i + 5760])
    assert [f.bits for f in out] == tbs


def test_r2d_lost_in_noise_and_found_above_it():
    """No frame decodes when noise swamps it; nearly all do 10 dB above it."""
    rng = np.random.default_rng(7)
    tb = [int(b) for b in rng.integers(0, 2, 96)]
    lost = sum(len(_r2d_loop(tb, 6, 1, 30.0, rng)[0]) for _ in range(5))
    found = sum(len(_r2d_loop(tb, 6, 1, 0.1, rng)[0]) for _ in range(10))
    assert lost == 0 and found >= 9


def test_r2d_m24_forces_ones():
    chips = aiot.r2d_chips([1] * 30, 24)
    for c in range(params.N_SIP, len(chips)):
        if (c - params.N_SIP) % 24 >= 22:
            assert chips[c] == 1
    assert (len(chips) - params.N_SIP) % 24 == 0


def test_r2d_waveform_stays_in_band():
    """The R2D only occupies its N_RB x 12 subcarriers (§6.2.7)."""
    x = aiot.r2d_waveform(aiot.r2d_chips([0, 1] * 20, 6), 6, 1)
    start = params.symbol_starts(3)[2] + params.CP_SHORT  # the useful part of symbol 2
    spec = np.abs(np.fft.fft(x[start:start + params.N_FFT])) ** 2
    k = np.fft.fftfreq(params.N_FFT, 1 / params.N_FFT)
    inside = (k >= -6) & (k < 6)
    assert spec[~inside].sum() < 1e-20 * spec.sum()


# ----------------------------------------------------------------------------------------------
# D2R
# ----------------------------------------------------------------------------------------------

def _d2r_run(cfgs, rng, sic=True, noise=1.0, delays=None, te=300):
    rx = aiot.D2RReceiver(noise, sic, rng)
    tags = []
    for i, (p, amp, tb) in enumerate(cfgs):
        b = aiot.Backscatter(rng.uniform(0, 2 * np.pi))
        b.queue(tb, p, amp, delay=1000 + (delays[i] if delays else 0))
        tags.append(b)
    rx.expect(aiot.Expect(1000, te, len(cfgs[0][2]), cfgs[0][0], r_sfs=sorted({c[0].r_sfs for c in cfgs})))
    for _ in range(400):
        out = rx.receive(sum(b.next_block(np.ones(5760)) for b in tags))
        if out:
            return out[0]
    raise AssertionError("the expected D2R never completed")


def _tb(rng, n):
    return [int(b) for b in rng.integers(0, 2, n)]


@pytest.mark.parametrize("mod", ["bpsk", "ook"])
@pytest.mark.parametrize("coded,r_block,i_add", [(False, 1, False), (True, 1, False), (False, 2, True), (True, 2, True)])
def test_d2r_round_trip(mod, coded, r_block, i_add):
    rng = np.random.default_rng(11)
    p = params.D2RParams(modulation=mod, r_code=coded, r_block=r_block, i_add=i_add, i_bit=16 * 48)
    tb = _tb(rng, 48)
    o = _d2r_run([(p, 1.0, tb)], rng, delays=[123])
    assert [r.bits for r in o.decoded] == [tb] and o.collided == 0
    assert o.decoded[0].start == 1000 + 123


def test_d2r_two_shifts_at_once():
    rng = np.random.default_rng(12)
    p4, p8 = params.D2RParams(r_sfs=4), params.D2RParams(r_sfs=8)
    a, b = _tb(rng, 16), _tb(rng, 16)
    o = _d2r_run([(p4, 1.0, a), (p8, 1.0, b)], rng)
    assert sorted((r.r_sfs, r.bits) for r in o.decoded) == [(4, a), (8, b)] and o.collided == 0


def test_d2r_collision_with_and_without_sic():
    rng = np.random.default_rng(13)
    p = params.D2RParams()
    a, b = _tb(rng, 16), _tb(rng, 16)
    with_sic = _d2r_run([(p, 1.0, a), (p, 0.5, b)], rng, delays=[0, 37])
    assert sorted(r.bits for r in with_sic.decoded) == sorted([a, b])
    without = _d2r_run([(p, 1.0, a), (p, 0.5, b)], rng, sic=False, delays=[0, 37])
    assert [r.bits for r in without.decoded] == [a] and without.collided == 1


def test_d2r_fec_gain():
    """At an SNR where uncoded frames mostly fail, the TBCC recovers most of them."""
    rng = np.random.default_rng(14)
    plain = params.D2RParams(r_sfs=4)
    coded = params.D2RParams(r_sfs=4, r_code=True)
    amp = 0.12   # about 1.4 dB SNR per bit uncoded (96 samples per bit, noise 1 per sample)
    ok = {False: 0, True: 0}
    for _ in range(10):
        tb = _tb(rng, 40)
        for p in (plain, coded):
            o = _d2r_run([(p, amp, tb)], rng)
            ok[p.r_code] += [r.bits for r in o.decoded] == [tb]
    assert ok[True] >= 8 and ok[True] > ok[False]


def test_d2r_rrm_9a_msg1_shape():
    """The 38.191 §9A D2R: bit 2 tau, R_SFS 8, long ambles every 48 bits, FEC; Msg1 is 16 bits."""
    p = params.D2RParams(t_bit_tau=Fraction(2), r_sfs=8, r_code=True, i_bit=48)
    assert params.frame_bits(16, p) == 128
    assert params.frame_bits(96, p) == 584
    assert p.chip_samples * 2 * 8 == p.bit_samples == 1536
