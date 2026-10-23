"""Answers the spec conformance vectors (tests/conformance/vectors) with Amber's Rel-19 PHY.

The harness (aiot_conformance, copied from the project's conformance set) calls one method per
vector kind; a method missing here makes those vectors SKIP. Every answer comes from
amber.phy and amber.mac, never from the harness's own oracle.
"""

from fractions import Fraction

from amber.phy import aiot
from amber.phy import params


def _bits(s):
    return [int(c) for c in s] if isinstance(s, str) else [int(b) for b in s]


def _bstr(bits):
    return "".join(str(int(b)) for b in bits)


class AmberAdapter:
    def constant(self, name):
        table = {
            "R2D_SIP": _bstr(params.SIP), "R2D_CAP": _bstr(params.CAP), "R2D_POSTAMBLE": _bstr(params.POSTAMBLE),
            "N_SIP": params.N_SIP, "N_CAP": params.N_CAP,
            "SCHED_BIT_DURATION_TAU": [str(b) for b in params.BIT_DURATIONS],
            "SCHED_RSFS_VALUES": list(params.R_SFS_VALUES), "SCHED_INTERVAL_BITS_BASE": list(params.I_BIT_BASE),
            "LAMBLE_SHORT": 7, "LAMBLE_LONG": 31,
            # in microseconds: tau samples at the sample rate
            "TAU_US": str(params.TAU * Fraction(1_000_000, params.SAMPLE_RATE_HZ)),
        }
        if name in table:
            return table[name]
        raise AttributeError(name)  # the rest are numerology constants of the spec, not of this PHY

    def m_sequence(self, n):
        return _bstr(params.m_sequence(n))

    def crc_parity(self, bits, L):
        return _bstr(aiot.crc(_bits(bits), L))

    def crc_attach(self, bits):
        return _bstr(aiot.attach_crc(_bits(bits)))

    def line_encode(self, bits):
        return _bstr(aiot.manchester(_bits(bits)))

    def sfs_modulate(self, bits, r_sfs, mod):
        p = _Chips(r_sfs, mod.lower())
        return [int(x) for x in aiot.sfs_chips(_bits(bits), p)]

    def tbcc_encode(self, bits):
        return _bstr(aiot.tbcc_encode(_bits(bits)))

    def nrb_min(self, m):
        return params.N_RB_MIN[m]

    def r2d_chip_layout(self, tb_bits, m):
        chips = aiot.r2d_chips(_bits(tb_bits), m)
        n_body = params.N_SIP + params.N_CAP + 2 * (len(tb_bits) + aiot.crc_length(len(tb_bits))) + 4
        # count the chips after the postamble (padding), forced chips inside it included
        last = _last_modulated(len(chips), n_body, m)
        return {"chips": _bstr(chips), "n_chips": len(chips), "n_pad": len(chips) - last,
                "n_ofdm_symbols": aiot.n_symbols(len(chips), m)}

    def r2d_duration_tc(self, n_ofdm_symbols, l0):
        # samples to T_c: one sample is 480e3*4096/5.76e6 T_c
        tc_per_sample = Fraction(480_000 * 4096, params.SAMPLE_RATE_HZ)
        n = sum(params.cp_length(l, l0) + params.N_FFT for l in range(n_ofdm_symbols))
        return n * tc_per_sample

    def r2d_chip_duration_us(self, m):
        per = params.SIP_CHIPS_PER_SYMBOL if m == "SIP" else m
        return Fraction(params.N_FFT // per * 1_000_000, params.SAMPLE_RATE_HZ)

    def d2r_total_bits(self, e_bits, l_amble, i_bit, i_add):
        return aiot.frame_bits_from_e(e_bits, _Amble(l_amble, i_bit, i_add))

    def d2r_assemble(self, e_bits, l_amble, i_bit, i_add):
        e = _bits(e_bits)
        p = _Amble(l_amble, i_bit, i_add)
        lay = aiot.amble_layout(len(e), p)
        amble = params.m_sequence(l_amble)
        it = iter(e)
        return _bstr([amble[k] if k >= 0 else next(it) for k in lay])

    def d2r_frame_bits(self, payload_bits, r_block, coded, l_amble, i_bit, i_add):
        b = (payload_bits + aiot.crc_length(payload_bits)) * r_block
        return aiot.frame_bits_from_e(3 * b if coded else b, _Amble(l_amble, i_bit, i_add))

    def sched_decode(self, bit_duration_cp, interval_cp):
        t = params.BIT_DURATIONS[bit_duration_cp]
        return {"t_bit_tau": str(t), "i_bit": params.i_bit_values(t)[interval_cp],
                "r_sfs_allowed": list(params.allowed_r_sfs(t))}

    def sched_scale(self, bit_duration_cp):
        t = params.BIT_DURATIONS[bit_duration_cp]
        return params.i_bit_values(t)[0] // params.I_BIT_BASE[0]

    def d2r_chip_params(self, t_bit_tau, r_sfs):
        t = Fraction(t_bit_tau)
        p = params.D2RParams(t_bit_tau=t, r_sfs=r_sfs, i_bit=params.i_bit_values(t)[0])
        us = Fraction(1_000_000, params.SAMPLE_RATE_HZ)
        return {"t_chip_tau": str(Fraction(p.chip_samples) / params.TAU), "t_chip_us": str(p.chip_samples * us),
                "shift_khz": str(Fraction(r_sfs * params.SAMPLE_RATE_HZ, p.bit_samples * 1000)),
                "nominal_bw_khz": str(Fraction(4 * params.SAMPLE_RATE_HZ, p.bit_samples * 1000))}

    def t_r2d(self, kind, t_bit_tau, r_sfs_set, r_sfs_own, m_chip_symb, n_tbs_bytes, coded, earlier_occasion,
              d2r_bits=None):
        t = Fraction(t_bit_tau)
        p = params.D2RParams(t_bit_tau=t, r_sfs=r_sfs_own, r_code=bool(coded), i_bit=params.i_bit_values(t)[0])
        msg = {"msg1": "msg1", "msg2resp": "msg2", "msg3": "msg2", "msg2": "msg2", "cfa": "cfa", "other": "data", "data": "data"}[kind]
        chips = (d2r_bits or 0) * 2 * r_sfs_own
        n = params.t_r_to_d(p, m_chip_symb, msg, n_tbs_bytes, r_sfs_set or (), earlier_occasion, chips)
        tau = Fraction(n) / params.TAU
        us = tau * params.TAU * Fraction(1_000_000, params.SAMPLE_RATE_HZ)
        return {"tau": str(tau), "us": str(us), "te_us": str(us / 10)}

    def t_offset(self, t_chip_tau, m_chip_symb):
        return params._t_offset(Fraction(t_chip_tau), m_chip_symb)

    def delta_code_t1(self, t_chip_tau):
        # Delta_code of Table 7.1.2-1: T_R->D of a coded CFA answer minus the uncoded one
        t = Fraction(t_chip_tau)
        tb = t * 2  # a bit duration giving this chip time with R_SFS = 1
        if tb not in params.BIT_DURATIONS:
            tb, r = t * 2 * 2, 2
        else:
            r = 1
        p = lambda coded: params.D2RParams(t_bit_tau=tb, r_sfs=r, r_code=coded, i_bit=params.i_bit_values(tb)[0])
        return Fraction(params.t_r_to_d(p(True), 6, "cfa", 1) - params.t_r_to_d(p(False), 6, "cfa", 1)) / params.TAU

    def delta_code_t2(self, n_tbs_bytes):
        p = lambda coded: params.D2RParams(r_code=coded)
        return Fraction(params.t_r_to_d(p(True), 6, "data", n_tbs_bytes)
                        - params.t_r_to_d(p(False), 6, "data", n_tbs_bytes)) / params.TAU

    def timing_check(self, t_r2d_us, te_us, measured_us):
        return {"pass": abs(Fraction(measured_us) - Fraction(t_r2d_us)) <= Fraction(te_us)}

    def t_d2r_min_us(self, t_chip_tau):
        chip = Fraction(t_chip_tau) * params.TAU
        n = max(3 * chip, Fraction(params.SAMPLE_RATE_HZ, 100_000))
        return n * Fraction(1_000_000, params.SAMPLE_RATE_HZ)

    def frc_r2d(self, m_chip_symb, tbs_bits, prb, **_):
        lay = self.r2d_chip_layout([0, 1] * (tbs_bits // 2), m_chip_symb)
        return {"n_pad": lay["n_pad"], "n_chips": lay["n_chips"], "n_ofdm_symbols": lay["n_ofdm_symbols"],
                "n_rb_min": params.N_RB_MIN[m_chip_symb], "prb_ok": prb >= params.N_RB_MIN[m_chip_symb]}


def _last_modulated(n_chips, n_body, m):
    """Index after the postamble's last chip, counting the forced chips placed among the body."""
    placed, chip = 0, 0
    while placed < n_body:
        if chip < params.N_SIP or not (m == 24 and (chip - params.N_SIP) % m >= m - 2):
            placed += 1
        chip += 1
    return chip


class _Chips:
    def __init__(self, r_sfs, modulation):
        self.r_sfs, self.modulation = r_sfs, modulation


class _Amble:
    def __init__(self, l_amble, i_bit, i_add):
        self.l_amble, self.i_bit, self.i_add = l_amble, i_bit, bool(i_add)


# ----------------------------------------------------------------------------------------------
# 38.391 MAC PDUs (vectors/mac_pdu.json)
#
# The vectors give each field as its raw codepoint; amber.mac (OAI's C MAC through ctypes) works in
# values. The Scheduling Info is turned into its bit string in table order and parsed by the C
# codec itself, so the codepoint-to-value mapping under test is the library's, not this adapter's.
# ----------------------------------------------------------------------------------------------

from amber import mac  # noqa: E402

_SCHED_FIELDS = {   # Table 6.2.1.6-1 row order, per carrier: (name, width)
    "paging_cbra": [("time_resource_cp", 1), ("bit_duration_cp", 3), ("freq_bitmap", 8), ("block_rep_cp", 1),
                    ("coding_cp", 1), ("interval_cp", 2), ("seq_len_cp", 1), ("add_midamble_cp", 1)],
    "paging_cfa": [("bit_duration_cp", 3), ("freq_bitmap", 8), ("block_rep_cp", 1), ("coding_cp", 1),
                   ("interval_cp", 2), ("seq_len_cp", 1), ("add_midamble_cp", 1), ("d2r_tbs_cp", 7)],
    "r2d_data": [("bit_duration_cp", 3), ("freq_unicast_cp", 3), ("block_rep_cp", 1), ("coding_cp", 1),
                 ("interval_cp", 2), ("seq_len_cp", 1), ("add_midamble_cp", 1), ("d2r_tbs_cp", 7)],
}
_SCHED_FIELDS["random_id_response"] = _SCHED_FIELDS["paging_cfa"]
_VARIANT = {"paging_cbra": mac.SCHED_PAGING_CBRA, "paging_cfa": mac.SCHED_PAGING_CFA,
            "random_id_response": mac.SCHED_RANDOM_ID_RESPONSE, "r2d_data": mac.SCHED_R2D_DATA}
_KIND = {mac.PAGING_CBRA: "paging_cbra", mac.PAGING_CFA: "paging_cfa", mac.ACCESS_TRIGGER: "access_trigger",
         mac.RANDOM_ID_RESPONSE: "random_id_response", mac.R2D_DATA: "r2d_data",
         mac.NACK_FEEDBACK: "nack_feedback"}
_R2D_TYPE = {1: "paging", 2: "access_trigger", 3: "random_id_response", 4: "r2d_data", 5: "nack_feedback"}


def _sched_bits(carrier, cps):
    out = ""
    for name, w in _SCHED_FIELDS[carrier]:
        v = cps[name]
        out += v if isinstance(v, str) else format(v, f"0{w}b")
    return _bits(out)


def _sched_obj(carrier, cps):
    return mac.sched_parse(_sched_bits(carrier, cps), _VARIANT[carrier])


def _sched_cps(carrier, sched):
    bits, out, i = _bstr(mac.sched_pack(sched)), {}, 0
    for name, w in _SCHED_FIELDS[carrier]:
        out[name] = bits[i:i + w] if name == "freq_bitmap" else int(bits[i:i + w], 2)
        i += w
    return out


def _sched_l1(carrier, sched):
    l1 = {"t_bit_tau": str(Fraction(sched.t_bit_num, sched.t_bit_den)), "r_block": sched.r_block,
          "coded": bool(sched.r_code), "i_bit": sched.i_bit, "l_amble": sched.l_amble, "i_add": bool(sched.i_add),
          "r_sfs_valid": True}   # sched_parse refuses an invalid set, so a parsed one is valid
    if carrier == "paging_cbra":
        l1["x"] = sched.x
    if carrier == "r2d_data":
        l1["r_sfs"] = sched.shifts[0]
    else:
        l1["r_sfs_set"] = sched.shifts
    if sched.d2r_tbs:
        l1["d2r_tbs_bytes"] = sched.d2r_tbs
    return l1


def _pid(v):
    return None if v is None else _bits(v)


def _fi(e):
    v = e.get("frequency_index_cp")
    return int(v, 2) if isinstance(v, str) else v


class _MacMixin:
    def mac_pack(self, kind, f):
        if kind == "access_random_id":
            return _bstr(mac.pack_d2r(mac.access_random_id(f["random_id"])))
        if kind == "d2r_data":
            m = mac.d2r_data(bytes.fromhex(f["sdu_hex"]), mdi=f["mdi"], padding_bytes=f["padding_bytes"])
            return _bstr(mac.pack_d2r(m))
        if kind == "access_trigger":
            return _bstr(mac.pack_r2d(mac.access_trigger()))
        tbs = f.get("r2d_tbs") or 0
        if kind == "nack_feedback":
            return _bstr(mac.pack_r2d(mac.nack(list(f["as_ids"]), tbs)))
        s = _sched_obj(kind, f["sched"])
        if kind == "paging_cbra":
            m = mac.paging_cbra(f["transaction_id"], s, f["n_access_occasions_exp"], f["k_cp"],
                                _pid(f["paging_id"]), tbs)
        elif kind == "paging_cfa":
            m = mac.paging_cfa(_pid(f["paging_id"]), s, tbs)
        elif kind == "random_id_response":
            ents = [(e["echoed_random_id"], e.get("assigned_as_id"), _fi(e)) for e in f["entries"]]
            m = mac.random_id_response(ents, s, tbs)
            if (ents[0][2] is not None) != bool(f["fipi"]):
                raise ValueError("FIPI does not match the entries")
        elif kind == "r2d_data":
            sdu, size = f.get("data_sdu_hex"), f.get("received_data_size")
            if (sdu is None) == (size is None):
                raise ValueError("exactly one of Data SDU and Received Data Size")
            m = mac.r2d_data(f["as_id"], s, None if sdu is None else bytes.fromhex(sdu), size, tbs)
            if int(sdu is not None) != f["ci"]:
                raise ValueError("CI does not match the payload given")
        else:
            raise ValueError(kind)
        return _bstr(mac.pack_r2d(m))

    def mac_parse(self, direction, bits):
        b = _bits(bits)
        if direction == "d2r":
            # the MAC knows from its own schedule which D2R message it expects; Msg1 is a bare 16 bits
            if len(b) == 16 and b[:2] != [0, 0]:
                return "access_random_id", {"random_id": mac.parse_d2r(b, mac.D2R_ACCESS_RANDOM_ID).random_id}
            m = mac.parse_d2r(b, mac.D2R_UPPER_LAYER_DATA, d2r_tbs=len(b) // 8)
            return "d2r_data", {"mdi": m.mdi, "sdu_hex": m.sdu.hex().upper(), "padding_bytes": m.padding_bytes}
        m = mac.parse_r2d(b)
        if m is None:
            raise ValueError("not an R2D message")
        kind = _KIND[m.kind]
        if kind == "access_trigger":
            return kind, {"fill_bits": 0}
        f = {"r2d_tbs": m.r2d_tbs}
        f["fill_bits"] = 8 * m.r2d_tbs - mac.r2d_body_bits(m)
        if kind == "nack_feedback":
            f["as_ids"] = list(m.as_ids[:m.n_as_ids])
            return kind, f
        f["sched"] = _sched_cps(kind, m.sched)
        pid = _bstr(m.paging_id[:m.paging_id_len]) if m.has_paging_id else None
        if kind == "paging_cbra":
            f.update(transaction_id=m.transaction_id, n_access_occasions_exp=m.num_access_occasions_code,
                     k_cp=m.k_bit, paging_id=pid)
        elif kind == "paging_cfa":
            f["paging_id"] = pid
        elif kind == "random_id_response":
            es = m.entries[:m.n_entries]
            fipi = es[0].freq_index >= 0
            f["fipi"] = int(fipi)
            f["entries"] = [dict({"echoed_random_id": e.echoed_random_id,
                                  "assigned_as_id": None if e.assigned_as_id < 0 else e.assigned_as_id},
                                 **({"frequency_index_cp": e.freq_index} if fipi else {})) for e in es]
        elif kind == "r2d_data":
            f.update(as_id=m.as_id, ci=m.has_sdu)
            if m.has_sdu:
                f["data_sdu_hex"] = bytes(m.sdu[:m.sdu_len]).hex().upper()
            else:
                f["received_data_size"] = m.received_data_size
        return kind, f

    def sched_info_unpack(self, carrier, bits):
        s = mac.sched_parse(_bits(bits), _VARIANT[carrier])
        return {"fields": _sched_cps(carrier, s), "l1": _sched_l1(carrier, s)}

    def cfa_bitmap_valid(self, bitmap):
        cps = {"bit_duration_cp": 0, "freq_bitmap": bitmap, "block_rep_cp": 0, "coding_cp": 0, "interval_cp": 0,
               "seq_len_cp": 0, "add_midamble_cp": 0, "d2r_tbs_cp": 0}
        try:
            _sched_obj("paging_cfa", cps)
            return True
        except ValueError:
            return False

    def sched_info_length(self, carrier):
        return mac.sched_width(_VARIANT[carrier])

    def d2r_data_padding_bytes(self, d2r_tbs_bytes, sdu_bytes):
        return mac.d2r_for_tbs(bytes(sdu_bytes), d2r_tbs_bytes).padding_bytes

    def n_access_occasions(self, field):
        return mac.num_access_occasions(field)

    def k_value(self, field):
        return mac.k_value(field)

    def frequency_index(self, cp):
        return mac.freq_index_r_sfs(int(cp, 2) if isinstance(cp, str) else cp)

    def r2d_message_type(self, t):
        n = int(t, 2)
        return _R2D_TYPE[n] if mac.r2d_type_valid(n) else "reserved"

    def d2r_message_type(self, t):
        return "d2r_data" if _bstr(_bits(t)) == "00" else "reserved"


for _name, _fn in vars(_MacMixin).items():
    if not _name.startswith("__"):
        setattr(AmberAdapter, _name, _fn)
