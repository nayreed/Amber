"""The RRM test case of TS 38.191 §9A, run on the link: one device, CBRA with one access occasion.

T1: the Reader sends an A-IoT Paging; the device must send Msg1 at T_R->D after its end, within
+-Te. T2: Msg1 lasts until T_D2R_min after its end. T3: the Reader sends Msg2 (Random ID
Response); the device must send Msg3 (D2R Upper Layer Data Transfer) at T_R->D after Msg2's end,
within +-Te. Parameters are those of Tables 9A.2.2-1 to -3.
"""

from fractions import Fraction

import numpy as np
import pytest

from amber import link, mac
from amber.phy import params

MS = params.SAMPLES_PER_MS
PAYLOAD = bytes(range(10))         # 96-bit TB: 2-byte header and a 10-byte SDU


class Up(mac.UpperLayer):
    def next_sdu(self):
        return PAYLOAD


def sched(variant, **kw):
    # Table 9A.2.2-3: bit 2 tau, R_SFS 8 (bitmap 00010000), L_amble 31, I_bit 48, FEC 1/3,
    # no additional midamble, block repetition 1
    return mac.sched(variant, (8,), t_bit_tau=Fraction(2), r_block=1, r_code=True, i_bit=48, l_amble=31,
                     i_add=False, **kw)


def offset(kind, p, tbs):
    """T_R->D (38.291 §7.1.2) of a D2R of tbs bytes on R_SFS 8, M = 2."""
    chips = params.frame_bits(8 * tbs, p) * 2 * p.r_sfs
    return params.t_r_to_d(p, 2, kind, tbs, (8,), False, chips)


def run_9a(timing_error, seed):
    def script():
        h = yield mac.paging_cbra(1, sched(mac.SCHED_PAGING_CBRA, x=1), n_occasions_code=0)   # 1 occasion
        entries = [(a.message.random_id, 1 + i, mac.R_SFS_VALUES.index(a.r_sfs)) for i, a in enumerate(h.answers)]
        yield mac.random_id_response(entries, sched(mac.SCHED_RANDOM_ID_RESPONSE, d2r_tbs=12))

    # Table 9A.2.2-2: 1 PRB, M = 2 chips per OFDM symbol
    dev = link.TagLink(mac.Device(Up(), np.random.default_rng(seed)), 1e-5, 1e-5, 1e-9,
                       np.random.default_rng(seed + 1), m_chip_symb=2, timing_error=timing_error)
    log = link.run_script(script(), [dev], m_chip_symb=2, n_rb=1, rng=np.random.default_rng(seed), max_ms=1000)
    return log, dev


@pytest.mark.parametrize("timing_error,seed", [(0.0, 1), (1.0, 2), (1.0, 3)])
def test_rrm_9a(timing_error, seed):
    (h1, h3), dev = run_9a(timing_error, seed)
    (msg1,), (msg3,) = h1.answers, h3.answers
    p = params.D2RParams(t_bit_tau=Fraction(2), r_sfs=8, r_code=True, i_bit=48)
    # Msg1: T_R->D after the paging, within +-Te (9A.2.3, 9.2.1)
    off1 = offset("msg1", p, 2)
    assert abs(msg1.start - (h1.r2d_end + off1)) <= params.timing_tolerance(off1)
    # Msg3: T_R->D after Msg2, within +-Te
    off3 = offset("msg2", p, 12)
    assert abs(msg3.start - (h3.r2d_end + off3)) <= params.timing_tolerance(off3)
    assert msg3.message.sdu == PAYLOAD
    # the frames have the §9A sizes: Msg1 128 bits, Msg3 584 bits, at 2 tau per bit
    assert params.frame_bits(16, p) == 128 and params.frame_bits(96, p) == 584
    # T1 = 10 ms holds the paging and the start of Msg1; Msg1 plus T_D2R_min fits T2 = 35 ms;
    # Msg3 fits T3 = 170 ms
    assert msg1.start - h1.r2d_start <= 10 * MS
    assert params.frame_samples(16, p) + params.t_d2r_min(p) <= 35 * MS
    assert msg3.start - h3.r2d_start + params.frame_samples(96, p) <= 170 * MS
    # 9.3: the Reader sent Msg2 no earlier than T_D2R_min after Msg1 ended
    assert h3.r2d_start >= msg1.start + params.frame_samples(16, p) + params.t_d2r_min(p)


def test_no_msg3_without_matching_msg2():
    """9.1.2.3: no D2R after a Random ID Response that does not echo the device's Random ID."""
    def script():
        yield mac.paging_cbra(1, sched(mac.SCHED_PAGING_CBRA, x=1))
        yield mac.random_id_response([(0xFFFF, 1, mac.R_SFS_VALUES.index(8))],
                                     sched(mac.SCHED_RANDOM_ID_RESPONSE, d2r_tbs=12))
    dev = link.TagLink(mac.Device(Up(), np.random.default_rng(5)), 1e-5, 1e-5, 1e-9, np.random.default_rng(6),
                       m_chip_symb=2)
    link.run_script(script(), [dev], m_chip_symb=2, n_rb=1, rng=np.random.default_rng(9), max_ms=1000)
    if dev.mac.state().random_id != 0xFFFF:
        assert [s.msg for _, s in dev.sent] == ["msg1"]
