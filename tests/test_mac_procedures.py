"""Tests of the 38.391 clause 5 procedures (amber.mac), without the air: messages go straight
from one MAC to the other, and a toy channel decides which D2R the Reader hears."""

from collections import Counter

import numpy as np
import pytest

from amber.mac import pdu as mp
from amber.phy import params
from amber.mac import DeviceMAC, Msg1Heard, ReaderMAC, Reassembly, UpperLayer, occasion_of

R = ReaderMAC


def cbra_sched(r_sfs=(8,), x=1):
    return R.sched(mp.SCHED_PAGING_CBRA, r_sfs, x=x, t_bit_tau=2, r_code=True)


def ridr_sched(r_sfs=(1, 2, 4, 8, 16, 32, 64, 128), tbs=12):
    return R.sched(mp.SCHED_RANDOM_ID_RESPONSE, r_sfs, d2r_tbs=tbs, t_bit_tau=2, r_code=True)


def data_sched(r_sfs=8, tbs=12):
    return R.sched(mp.SCHED_R2D_DATA, (r_sfs,), d2r_tbs=tbs, t_bit_tau=2, r_code=True)


class Upper(UpperLayer):
    def __init__(self, sdus=(), pid=None):
        self.sdus, self.pid, self.got = list(sdus), pid, []

    def paging_id_matched(self, paging_id):
        return self.pid is None or tuple(paging_id) == tuple(self.pid)

    def deliver(self, sdu):
        self.got.append(sdu)

    def next_sdu(self):
        return self.sdus.pop(0) if self.sdus else None


def dev(seed=0, **kw):
    return DeviceMAC(Upper(**kw), np.random.default_rng(seed))


def heard(s: list):
    """What the Reader hears from a lone Msg1 send."""
    (d,) = s
    return Msg1Heard(d.message.random_id, d.params.r_sfs, d.slot)


# ----------------------------------------------------------------------------------------------

def test_cbra_one_device_end_to_end():
    d, r = dev(sdus=[b"hello"]), ReaderMAC()
    s = d.on_r2d(r.paging_cbra(7, cbra_sched()))
    assert len(s) == 1 and s[0].msg == "msg1" and s[0].params.r_sfs == 8 and s[0].slot == 0
    assert len(s[0].pdu) == 16
    ridr = r.random_id_response([heard(s)], ridr_sched())
    s = d.on_r2d(mp.parse_r2d(ridr.pack()))           # through the bits, as on the air
    assert d.cbra_success and d.as_id == 1
    (m3,) = s
    assert m3.msg == "msg2" and m3.params.r_sfs == 1 and m3.tbs_bytes == 12
    got = mp.parse_d2r(m3.pdu, "upper_layer_data", d2r_tbs=12)
    assert got.sdu == b"hello" and got.mdi == 0 and got.padding_bytes == 5
    # the same Transaction ID again: already done, the device stays quiet
    assert d.on_r2d(r.paging_cbra(7, cbra_sched())) == []
    # a new one starts a fresh access and drops the AS ID
    assert d.on_r2d(r.paging_cbra(8, cbra_sched()))[0].msg == "msg1" and d.as_id is None


def test_t_r_to_d_accepts_what_the_mac_asks_for():
    d, r = dev(1, sdus=[b"x" * 9]), ReaderMAC()
    (s1,) = d.on_r2d(r.paging_cbra(1, cbra_sched((4, 8), x=2), n_occasions_code=2))
    (s3,) = d.on_r2d(r.random_id_response([Msg1Heard(s1.message.random_id, s1.params.r_sfs, s1.slot)],
                                          ridr_sched()))
    for s in (s1, s3):
        n = params.t_r_to_d(s.params, 6, s.msg, s.tbs_bytes, s.candidate_r_sfs, s.earlier_occasion,
                            params.frame_bits(8 * s.tbs_bytes, s.params) * 2 * s.params.r_sfs)
        assert n > 0


@pytest.mark.parametrize("seed", range(12))
def test_access_occasion_count_down(seed):
    """n = 16 occasions, m = X * N_SFS = 4 per R2D: counter c answers the (c // 4)-th Access Trigger
    (0 = the paging itself) on occasion c % 4."""
    d, r = dev(seed), ReaderMAC()
    sched = cbra_sched((2, 8), x=2)
    out = [d.on_r2d(r.paging_cbra(1, sched, n_occasions_code=4))]
    counter = int(d.log[0].split("counter=")[1])
    for _ in range(3):
        out.append(d.on_r2d(mp.AccessTrigger()))
    sent = [i for i, o in enumerate(out) if o]
    assert sent == [counter // 4]
    (s,) = out[counter // 4]
    assert (s.slot, s.params.r_sfs) == occasion_of(sched, counter % 4)


def test_counter_spreads_over_all_occasions():
    seen = Counter()
    for seed in range(400):
        d = dev(seed)
        d.on_r2d(ReaderMAC().paging_cbra(1, cbra_sched(), n_occasions_code=3))
        seen[int(d.log[0].split("counter=")[1])] += 1
    assert set(seen) == set(range(8)) and min(seen.values()) > 25


def test_msg2_window_is_k_triggers():
    """5.3.1.3: after K Access Triggers (or pagings) the device stops listening for Msg2."""
    for k_bit, k in ((0, 1), (1, 4)):
        d, r = dev(3), ReaderMAC()
        (s,) = d.on_r2d(r.paging_cbra(1, cbra_sched(), k_bit=k_bit))
        for _ in range(k):
            d.on_r2d(mp.AccessTrigger())
        assert d.on_r2d(r.random_id_response([heard([s])], ridr_sched())) == []
        d, r = dev(3), ReaderMAC()
        (s,) = d.on_r2d(r.paging_cbra(1, cbra_sched(), k_bit=k_bit))
        for _ in range(k - 1):
            d.on_r2d(mp.AccessTrigger())
        assert len(d.on_r2d(r.random_id_response([heard([s])], ridr_sched()))) == 1


def test_msg2_matching_rules():
    d, r = dev(4), ReaderMAC()
    (s,) = d.on_r2d(r.paging_cbra(1, cbra_sched((4, 8))))
    rid, sfs = s.message.random_id, s.params.r_sfs
    other = 4 if sfs == 8 else 8
    # right ID, wrong Frequency Index: not mine
    assert d.on_r2d(r.random_id_response([Msg1Heard(rid, other)], ridr_sched())) == []
    # someone else's ID: not mine
    assert d.on_r2d(r.random_id_response([Msg1Heard(rid ^ 1, sfs)], ridr_sched())) == []
    # FIPI = 0, no AS ID assigned: match on the ID alone, and the AS ID is the Random ID
    (m3,) = d.on_r2d(r.random_id_response([Msg1Heard(rid ^ 1, sfs), Msg1Heard(rid, sfs)], ridr_sched(),
                                          assign=False, with_freq=False))
    assert d.as_id == rid and m3.params.r_sfs == 2           # second entry -> second bitmap value
    # a later Random ID Response naming the stored AS ID asks for the data again
    assert len(d.on_r2d(mp.RandomIDResponse(ridr_sched(), [mp.RandomIDEntry(rid)]))) == 1


def test_failed_cbra_retries_same_transaction():
    """5.5.3 then 5.2: no Msg2 before the next paging means failure, so the same TID starts again."""
    d, r = dev(5), ReaderMAC()
    assert d.on_r2d(r.paging_cbra(9, cbra_sched()))
    assert d.on_r2d(r.paging_cbra(9, cbra_sched()))
    assert any("5.5.3" in x for x in d.log)


def test_nack_makes_the_device_retry():
    d, r = dev(6, sdus=[b"a", b"b"]), ReaderMAC()
    (s,) = d.on_r2d(r.paging_cbra(2, cbra_sched()))
    d.on_r2d(r.random_id_response([heard([s])], ridr_sched()))
    d.on_r2d(r.nack([0x7777]))                               # someone else
    assert d.as_id == 1
    d.on_r2d(r.nack([1]))
    assert d.as_id is None and d.failed_tid == 2
    assert d.on_r2d(r.paging_cbra(2, cbra_sched()))[0].msg == "msg1"


def test_nack_ignored_after_r2d_data():
    d, r = dev(7, sdus=[b"a", b"b"]), ReaderMAC()
    (s,) = d.on_r2d(r.paging_cbra(2, cbra_sched()))
    d.on_r2d(r.random_id_response([heard([s])], ridr_sched()))
    assert len(d.on_r2d(r.data(1, data_sched(), sdu=b"cmd"))) == 1
    d.on_r2d(r.nack([1]))
    assert d.as_id == 1 and d.upper.got == [b"cmd"]


def test_cfa():
    pid = [1, 0, 1, 1, 0, 0, 1, 0]
    d, r = dev(8, sdus=[b"meter=42"], pid=pid), ReaderMAC()
    cfa = R.sched(mp.SCHED_PAGING_CFA, (16,), d2r_tbs=10, t_bit_tau=1)
    assert d.on_r2d(r.paging_cfa([0] * 8, cfa)) == []        # not my Paging ID
    (s,) = d.on_r2d(r.paging_cfa(pid, cfa))
    assert s.msg == "cfa" and s.params.r_sfs == 16 and s.tbs_bytes == 10
    assert mp.parse_d2r(s.pdu, "upper_layer_data", 10).sdu == b"meter=42"
    # 5.4.3: with no AS ID after CFA, the first R2D data addressed to anyone gives the AS ID
    d.upper.sdus = [b"ok"]
    (s,) = d.on_r2d(r.data(0x0BEE, data_sched(), sdu=b"set 1"))
    assert d.as_id == 0x0BEE and d.upper.got == [b"set 1"]


def test_segmentation_round_trip():
    sdu = bytes(range(35))
    d, r = dev(9, sdus=[sdu]), ReaderMAC()
    cfa = R.sched(mp.SCHED_PAGING_CFA, (8,), d2r_tbs=12, t_bit_tau=2)
    (s,) = d.on_r2d(r.paging_cfa([1], cfa))
    re = Reassembly()
    d.as_id = 5                                              # as if assigned earlier
    whole, n = re.add(5, s.message)
    assert whole is None and n == 10 and s.message.mdi == 1
    while whole is None:
        (s,) = d.on_r2d(r.data(5, data_sched(tbs=12), received=n))
        whole, n = re.add(5, s.message)
    assert whole == sdu


def test_r2d_data_not_for_me():
    d, r = dev(10), ReaderMAC()
    d.as_id = 3
    assert d.on_r2d(r.data(4, data_sched(), sdu=b"x")) == []
    assert d.upper.got == []


# ----------------------------------------------------------------------------------------------
# A whole inventory: many devices, one Reader, collisions on shared occasions
# ----------------------------------------------------------------------------------------------

def run_inventory(n_dev, n_code, sfs, x, seed, max_rounds=30):
    """The Reader pages with one Transaction ID, then sends Access Triggers until every occasion has
    passed, answering each R2D's singles with one Random ID Response. Occasions picked by more than
    one device are lost (no capture). It pages again, same TID, until nobody answers."""
    rng = np.random.default_rng(seed)
    devs = [DeviceMAC(Upper(sdus=[f"tag{i}".encode()]), rng) for i in range(n_dev)]
    r = ReaderMAC()
    read, collisions, r2d_count = {}, 0, 0
    sched = cbra_sched(sfs, x)
    per_r2d = x * len(sfs)
    triggers = -(-(1 << n_code) // per_r2d) - 1
    for _ in range(max_rounds):
        quiet = True
        for t in range(triggers + 1):
            msg = r.paging_cbra(1, sched, n_occasions_code=n_code, k_bit=0) if t == 0 else mp.AccessTrigger()
            r2d_count += 1
            sends = {i: dv.on_r2d(msg) for i, dv in enumerate(devs)}
            occ = {}
            for i, s in sends.items():
                for a in s:
                    occ.setdefault((a.slot, a.params.r_sfs), []).append((i, a))
            quiet &= not occ
            singles = [v[0] for v in occ.values() if len(v) == 1]
            collisions += sum(1 for v in occ.values() if len(v) > 1)
            if not singles:
                continue
            singles = singles[:mp.MAX_ID_ENTRIES]
            ridr = r.random_id_response([Msg1Heard(a.message.random_id, a.params.r_sfs, a.slot)
                                         for _, a in singles], ridr_sched())
            r2d_count += 1
            for i, dv in enumerate(devs):
                for a in dv.on_r2d(ridr):
                    got = mp.parse_d2r(a.pdu, "upper_layer_data", a.tbs_bytes)
                    read[dv.as_id] = got.sdu
        if quiet:
            break
    return read, collisions, r2d_count


def test_inventory_reads_every_device():
    read, collisions, _ = run_inventory(20, 3, (1, 2, 4, 8), 2, seed=1)
    assert sorted(read.values()) == sorted(f"tag{i}".encode() for i in range(20))
    assert collisions > 0                                    # 20 devices on 8 occasions must collide


def test_inventory_wider_frame_collides_less():
    narrow = sum(run_inventory(16, 2, (8,), 1, seed=s)[1] for s in range(5))
    wide = sum(run_inventory(16, 5, (8,), 1, seed=s)[1] for s in range(5))
    assert wide < narrow
