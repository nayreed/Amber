"""38.391 messages over real waveforms (amber.link): Reader policy, Tag MAC, PHY in between."""

from fractions import Fraction

import numpy as np
import pytest

from amber import link
from amber.mac import pdu as mp
from amber.phy import params
from amber.mac import DeviceMAC, Msg1Heard, ReaderMAC, UpperLayer

S = ReaderMAC.sched


class Up(UpperLayer):
    def __init__(self, sdu=None, pid=None):
        self.sdu, self.pid, self.got = sdu, pid, []

    def paging_id_matched(self, paging_id):
        return self.pid is None or tuple(paging_id) == tuple(self.pid)

    def next_sdu(self):
        return self.sdu

    def deliver(self, sdu):
        self.got.append(sdu)


def tag(i, sdu=None, incident_w=1e-5, amp=1e-5, timing_error=0.5, pid=None):
    return link.TagLink(DeviceMAC(Up(sdu, pid), np.random.default_rng(100 + i)), incident_w, amp, 1e-9,
                        np.random.default_rng(200 + i), timing_error=timing_error)


def run(policy, tags, sic=True):
    log = []

    def wrapped():
        g = policy()
        h = None
        try:
            msg = next(g)
            while True:
                h = yield msg
                log.append(h)
                msg = g.send(h)
        except StopIteration:
            return
    r = link.ReaderLink(wrapped(), m_chip_symb=6, noise_w=1e-13, enable_sic=sic, rng=np.random.default_rng(1))
    link.run_link(r, tags, 3000)
    return log


CB = S(mp.SCHED_PAGING_CBRA, (4, 8), x=1, t_bit_tau=1, r_code=True)
RS = S(mp.SCHED_RANDOM_ID_RESPONSE, (1, 2, 4, 8), d2r_tbs=8, t_bit_tau=1, r_code=True)


def inventory_once():
    r = ReaderMAC()
    h = yield r.paging_cbra(1, CB, n_occasions_code=1)
    heard = [Msg1Heard(a.message.random_id, a.r_sfs, a.slot) for a in h.answers]
    if heard:
        yield r.random_id_response(heard, RS)


def test_two_tags_cbra_over_the_air():
    tags = [tag(0, b"t0"), tag(1, b"t1")]
    log = run(inventory_once, tags)
    msg1, data = log
    assert sorted(a.r_sfs for a in msg1.answers) == [4, 8] and msg1.collided == 0
    assert sorted(a.message.sdu for a in data.answers) == [b"t0", b"t1"]
    for t in tags:
        (s1, a), (s3, b) = t.sent
        assert a.msg == "msg1" and b.msg == "msg2" and not t.missed
    # every D2R started within +-Te of T_R->D after the R2D it answers (38.191 §9.2.1)
    for h, kind in ((msg1, "msg1"), (data, "msg2")):
        for a in h.answers:
            p = params.D2RParams(t_bit_tau=1, r_sfs=a.r_sfs, r_code=True, i_bit=96)
            tbs = 2 if kind == "msg1" else 8
            off = link.d2r_start_offset(kind, p, 6, tbs, (4, 8) if kind == "msg1" else RS.r_sfs)
            assert abs(a.start - (h.r2d_end + off)) <= params.timing_tolerance(off)


def test_no_d2r_without_msg2():
    """38.191 §9.1.2.3: a device whose Msg1 got no matching Msg2 sends no data."""
    def policy():
        r = ReaderMAC()
        yield r.paging_cbra(1, CB)
        yield r.random_id_response([Msg1Heard(0x1234, 4)], RS)       # nobody's ID
    t = tag(0, b"x")
    log = run(policy, [t])
    assert [s.msg for _, s in t.sent] == ["msg1"] and log[1].empty


def test_tag_out_of_range_hears_nothing():
    t = tag(0, b"x", incident_w=1e-12)
    log = run(inventory_once, [t])
    assert t.sent == [] and log[0].empty


def test_cfa_and_r2d_data_over_the_air():
    pid = [1, 0, 1, 0, 1, 1, 0, 0]
    t = tag(0, b"hi", pid=pid)
    other = tag(1, b"no", pid=[0] * 8)

    def policy():
        r = ReaderMAC()
        h = yield r.paging_cfa(pid, S(mp.SCHED_PAGING_CFA, (8,), d2r_tbs=6, t_bit_tau=1))
        assert [a.message.sdu for a in h.answers] == [b"hi"]
        h = yield r.data(0x0042, S(mp.SCHED_R2D_DATA, (16,), d2r_tbs=6, t_bit_tau=Fraction(1, 2)), sdu=b"go")
        assert [(a.message.sdu, a.r_sfs) for a in h.answers] == [(b"hi", 16)]
    run(policy, [t, other])
    assert t.mac.as_id == 0x0042
    assert t.mac.upper.got == [b"go"] and other.sent == []


def test_two_slots_start_where_the_reader_looks():
    """X = 2: the second time slot starts at 1.25 (T_offset + M_chip T'_chip) after the R2D."""
    cb = S(mp.SCHED_PAGING_CBRA, (8,), x=2, t_bit_tau=1, r_code=True)
    found = set()
    for seed in range(6):
        t = link.TagLink(DeviceMAC(Up(b"x"), np.random.default_rng(seed)), 1e-5, 1e-5, 1e-9,
                         np.random.default_rng(seed), timing_error=0.5)

        def policy():
            yield ReaderMAC().paging_cbra(1, cb, n_occasions_code=1)
        (h,) = run(policy, [t])
        (a,) = h.answers
        found.add(a.slot)
        assert a.slot == t.sent[0][1].slot
    assert found == {0, 1}


@pytest.mark.parametrize("sic,expect", [(True, 2), (False, 1)])
def test_same_occasion_collision(sic, expect):
    """Two Tags on the one occasion: SIC separates them when one is clearly stronger."""
    cb = S(mp.SCHED_PAGING_CBRA, (8,), x=1, t_bit_tau=1, r_code=True)

    def policy():
        yield ReaderMAC().paging_cbra(1, cb)
    tags = [tag(0, amp=1e-5), tag(1, amp=4e-6)]
    (h,) = run(policy, tags, sic=sic)
    assert len(h.answers) == expect and len(h.answers) + h.collided == 2
