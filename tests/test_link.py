"""38.391 messages over real waveforms (amber.link): the Reader's MAC, the Tags' MAC, the PHY in
between. The MAC is OAI's C library (amber.mac); each test scripts the Reader's messages with the
protocol "manual", which sends only what it is given."""

from fractions import Fraction

import numpy as np
import pytest

from amber import link, mac
from amber.phy import params

LINK = link.LinkConfig(m_chip_symb=6, n_rb=0, t_bit_tau="1", r_sfs=(4, 8), x=1, r_code=True, r_block=1,
                       l_amble=31, interval_cp=0, i_add=False, d2r_tbs=8, modulation="bpsk", timing_error=0.5,
                       tag_snr_db=10.0)


class Up(mac.UpperLayer):
    def __init__(self, sdu=None, pid=None):
        self.sdu, self.pid, self.got = sdu, pid, []

    def paging_id_matched(self, paging_id):
        return self.pid is None or tuple(paging_id) == tuple(self.pid)

    def next_sdu(self):
        return self.sdu

    def deliver(self, sdu):
        self.got.append(sdu)


def tag(i, sdu=None, incident_w=1e-5, amp=1e-5, timing_error=0.5, pid=None):
    return link.TagLink(mac.Device(Up(sdu, pid), np.random.default_rng(100 + i)), incident_w, amp, 1e-9,
                        np.random.default_rng(200 + i), timing_error=timing_error)


def run(script, tags, sic=True, max_ms=3000):
    return link.run_script(script(), tags, LINK, enable_sic=sic, rng=np.random.default_rng(1), max_ms=max_ms)


CB = mac.sched(mac.SCHED_PAGING_CBRA, (4, 8), x=1, t_bit_tau=1, r_code=True)
RS = mac.sched(mac.SCHED_RANDOM_ID_RESPONSE, (1, 2, 4, 8), d2r_tbs=8, t_bit_tau=1, r_code=True)


def answer(heard, next_as_id=1):
    """A Random ID Response to every Msg1 heard, assigning AS IDs from next_as_id."""
    return mac.random_id_response([(a.message.random_id, next_as_id + i, mac.R_SFS_VALUES.index(a.r_sfs))
                                   for i, a in enumerate(heard.answers)], RS)


def inventory_once():
    h = yield mac.paging_cbra(1, CB, n_occasions_code=1)
    if h.answers:
        yield answer(h)


def offset(kind, p, tbs, candidates):
    """T_R->D of a D2R, from amber.phy.params (38.291 §7.1.2), for checking where it started."""
    chips = params.frame_bits(8 * tbs, p) * 2 * p.r_sfs
    return params.t_r_to_d(p, 6, kind, tbs, candidates, False, chips)


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
            off = offset(kind, p, tbs, (4, 8) if kind == "msg1" else RS.shifts)
            assert abs(a.start - (h.r2d_end + off)) <= params.timing_tolerance(off)


def test_reader_answers_msg1s_of_an_access_message():
    """A paging pushed as an access message gets the Reader's own Random ID Response."""
    tags = [tag(0, b"t0"), tag(1, b"t1")]
    reader = mac.Reader(LINK, "manual", {}, [])
    reader.push(mac.paging_cbra(1, mac.sched(mac.SCHED_PAGING_CBRA, (4, 8), x=1, t_bit_tau=1, r_code=True),
                                n_occasions_code=1), access=True)
    log = []
    r = link.ReaderLink(reader, m_chip_symb=6, noise_w=1e-13, rng=np.random.default_rng(1), on_heard=log.append)
    for _ in range(3000):
        if len(log) == 2:
            break
        carrier = r.next_block()
        r.receive(sum((t.step(carrier) for t in tags), np.zeros(link.BLOCK, dtype=np.complex128)))
    page, ridr = log
    assert ridr.sent.kind == mac.RANDOM_ID_RESPONSE and ridr.sent.n_entries == len(page.answers) == 2
    assert [e.assigned_as_id for e in ridr.sent.entries[:2]] == [1, 2]
    assert sorted(a.message.sdu for a in ridr.answers) == [b"t0", b"t1"]
    assert sorted(t.mac.state().as_id for t in tags) == [1, 2]


def test_no_d2r_without_msg2():
    """38.191 §9.1.2.3: a device whose Msg1 got no matching Msg2 sends no data."""
    def script():
        yield mac.paging_cbra(1, CB)
        yield mac.random_id_response([(0x1234, 1, mac.R_SFS_VALUES.index(4))], RS)     # nobody's ID
    t = tag(0, b"x")
    log = run(script, [t])
    assert [s.msg for _, s in t.sent] == ["msg1"] and log[1].empty


def test_tag_out_of_range_hears_nothing():
    t = tag(0, b"x", incident_w=1e-12)
    log = run(inventory_once, [t])
    assert t.sent == [] and log[0].empty


def test_cfa_and_r2d_data_over_the_air():
    pid = [1, 0, 1, 0, 1, 1, 0, 0]
    t = tag(0, b"hi", pid=pid)
    other = tag(1, b"no", pid=[0] * 8)

    def script():
        h = yield mac.paging_cfa(pid, mac.sched(mac.SCHED_PAGING_CFA, (8,), d2r_tbs=6, t_bit_tau=1))
        assert [a.message.sdu for a in h.answers] == [b"hi"]
        h = yield mac.r2d_data(0x0042, mac.sched(mac.SCHED_R2D_DATA, (16,), d2r_tbs=6, t_bit_tau=Fraction(1, 2)),
                               sdu=b"go")
        assert [(a.message.sdu, a.r_sfs) for a in h.answers] == [(b"hi", 16)]
    run(script, [t, other])
    assert t.mac.state().as_id == 0x0042
    assert t.mac.upper.got == [b"go"] and other.sent == []


def test_two_slots_start_where_the_reader_looks():
    """X = 2: the second time slot starts at 1.25 (T_offset + M_chip T'_chip) after the R2D."""
    cb = mac.sched(mac.SCHED_PAGING_CBRA, (8,), x=2, t_bit_tau=1, r_code=True)
    found = set()
    for seed in range(6):
        t = link.TagLink(mac.Device(Up(b"x"), np.random.default_rng(seed)), 1e-5, 1e-5, 1e-9,
                         np.random.default_rng(seed), timing_error=0.5)

        def script():
            yield mac.paging_cbra(1, cb, n_occasions_code=1)
        (h,) = run(script, [t])
        (a,) = h.answers
        found.add(a.slot)
        assert a.slot == t.sent[0][1].slot
    assert found == {0, 1}


@pytest.mark.parametrize("sic,expect", [(True, 2), (False, 1)])
def test_same_occasion_collision(sic, expect):
    """Two Tags on the one occasion: SIC separates them when one is clearly stronger."""
    cb = mac.sched(mac.SCHED_PAGING_CBRA, (8,), x=1, t_bit_tau=1, r_code=True)

    def script():
        yield mac.paging_cbra(1, cb)
    tags = [tag(0, amp=1e-5), tag(1, amp=4e-6)]
    (h,) = run(script, tags, sic=sic)
    assert len(h.answers) == expect and len(h.answers) + h.collided == 2


def test_link_config_is_checked_against_the_spec():
    with pytest.raises(ValueError, match="Table 6.2.1.6-1"):
        link.LinkConfig(m_chip_symb=6, n_rb=0, t_bit_tau="1/8", r_sfs=(1, 2, 4, 8, 16), x=1, r_code=True, r_block=1,
                        l_amble=31, interval_cp=0, i_add=False, d2r_tbs=8, modulation="bpsk", timing_error=0.5,
                        tag_snr_db=10.0)
