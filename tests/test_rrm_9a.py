"""The RRM test case of TS 38.191 §9A, run on the link: one device, CBRA with one access occasion.

T1: the Reader sends an A-IoT Paging; the device must send Msg1 at T_R->D after its end, within
+-Te. T2: Msg1 lasts until T_D2R_min after its end. T3: the Reader sends Msg2 (Random ID
Response); the device must send Msg3 (D2R Upper Layer Data Transfer) at T_R->D after Msg2's end,
within +-Te. Parameters are those of Tables 9A.2.2-1 to -3.
"""

from fractions import Fraction

import numpy as np
import pytest

from amber import link
from amber.mac import pdu as mp
from amber.phy import params
from amber.mac import DeviceMAC, Msg1Heard, ReaderMAC, UpperLayer

MS = params.SAMPLES_PER_MS
PAYLOAD = bytes(range(10))         # 96-bit TB: 2-byte header and a 10-byte SDU


class Up(UpperLayer):
    def next_sdu(self):
        return PAYLOAD


def sched(variant, **kw):
    # Table 9A.2.2-3: bit 2 tau, R_SFS 8 (bitmap 00010000), L_amble 31, I_bit 48, FEC 1/3,
    # no additional midamble, block repetition 1
    return mp.SchedulingInfo(variant, t_bit_tau=Fraction(2), r_sfs=(8,), r_block=1, r_code=True, i_bit=48,
                             l_amble=31, i_add=False, **kw)


def run_9a(timing_error, seed):
    log = []

    def policy():
        r = ReaderMAC()
        h = yield r.paging_cbra(1, sched(mp.SCHED_PAGING_CBRA, x=1), n_occasions_code=0)   # 1 occasion
        log.append(h)
        heard = [Msg1Heard(a.message.random_id, a.r_sfs, a.slot) for a in h.answers]
        h = yield r.random_id_response(heard, sched(mp.SCHED_RANDOM_ID_RESPONSE, d2r_tbs=12))
        log.append(h)

    # Table 9A.2.2-2: 1 PRB, M = 2 chips per OFDM symbol
    reader = link.ReaderLink(policy(), m_chip_symb=2, n_rb=1, noise_w=1e-13, rng=np.random.default_rng(seed))
    dev = link.TagLink(DeviceMAC(Up(), np.random.default_rng(seed)), 1e-5, 1e-5, 1e-9,
                       np.random.default_rng(seed + 1), m_chip_symb=2, timing_error=timing_error)
    link.run_link(reader, [dev], 1000)
    return log, dev


@pytest.mark.parametrize("timing_error,seed", [(0.0, 1), (1.0, 2), (1.0, 3)])
def test_rrm_9a(timing_error, seed):
    (h1, h3), dev = run_9a(timing_error, seed)
    (msg1,), (msg3,) = h1.answers, h3.answers
    p = params.D2RParams(t_bit_tau=Fraction(2), r_sfs=8, r_code=True, i_bit=48)
    # Msg1: T_R->D after the paging, within +-Te (9A.2.3, 9.2.1)
    off1 = link.d2r_start_offset("msg1", p, 2, 2, (8,))
    assert abs(msg1.start - (h1.r2d_end + off1)) <= params.timing_tolerance(off1)
    # Msg3: T_R->D after Msg2, within +-Te
    off3 = link.d2r_start_offset("msg2", p, 2, 12, (8,))
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
    def policy():
        r = ReaderMAC()
        yield r.paging_cbra(1, sched(mp.SCHED_PAGING_CBRA, x=1))
        yield r.random_id_response([Msg1Heard(0xFFFF, 8)], sched(mp.SCHED_RANDOM_ID_RESPONSE, d2r_tbs=12))
    reader = link.ReaderLink(policy(), m_chip_symb=2, n_rb=1, noise_w=1e-13, rng=np.random.default_rng(9))
    dev = link.TagLink(DeviceMAC(Up(), np.random.default_rng(5)), 1e-5, 1e-5, 1e-9, np.random.default_rng(6),
                       m_chip_symb=2)
    link.run_link(reader, [dev], 1000)
    if dev.mac.random_id != 0xFFFF:
        assert [s.msg for _, s in dev.sent] == ["msg1"]
