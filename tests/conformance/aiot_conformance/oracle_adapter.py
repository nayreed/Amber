"""Adapter that answers every vector kind from oracle.py (the spec reference model).

An implementation under test provides an object with the same method names;
any method it lacks makes the matching vectors SKIP rather than FAIL.
"""
from fractions import Fraction as F
from . import oracle as o
from . import oracle_mac as m


def _f(x):
    return F(x) if not isinstance(x, F) else x


class OracleAdapter:
    _CONST = {
        "R2D_SIP": lambda: o.bstr(o.SIP), "R2D_CAP": lambda: o.bstr(o.CAP),
        "R2D_POSTAMBLE": lambda: o.bstr(o.POSTAMBLE), "N_SIP": lambda: o.N_SIP, "N_CAP": lambda: o.N_CAP,
        "KAPPA": lambda: o.KAPPA, "TAU_US": lambda: str(o.TAU_US), "TC_US": lambda: str(o.TC_US),
        "N_U_OVER_KAPPA": lambda: o.N_U // o.KAPPA, "N_CP_L0_OVER_KAPPA": lambda: o.N_CP_L0 // o.KAPPA,
        "N_CP_OTHER_OVER_KAPPA": lambda: o.N_CP_OTHER // o.KAPPA,
        "SCHED_BIT_DURATION_TAU": lambda: [str(x) for x in o.TBIT_TAU],
        "SCHED_RSFS_VALUES": lambda: o.RSFS_ALL, "SCHED_INTERVAL_BITS_BASE": lambda: o.IBIT_BASE,
        "LAMBLE_SHORT": lambda: 7, "LAMBLE_LONG": lambda: 31,
    }

    def constant(self, name):
        if name in self._CONST:
            return self._CONST[name]()
        raise NotImplementedError(name)

    def m_sequence(self, n):
        return o.bstr(o.m_sequence(n))

    def crc_parity(self, bits, L):
        return o.bstr(o.crc_parity(o.bits(bits), L))

    def crc_attach(self, bits):
        return o.bstr(o.crc_attach(o.bits(bits)))

    def line_encode(self, bits):
        return o.bstr(o.line_encode(o.bits(bits)))

    def sfs_modulate(self, bits, r_sfs, mod):
        return o.sfs_modulate(o.bits(bits), r_sfs, mod)

    def r2d_ook_map(self, chips):
        return o.bits(chips)

    def tbcc_encode(self, bits):
        return o.bstr(o.tbcc_encode(o.bits(bits)))

    def nrb_min(self, m):
        return o.NRB_MIN[m]

    def r2d_chip_layout(self, tb_bits, m):
        ch, pad = o.r2d_chips(o.bits(tb_bits), m)
        return {"chips": "".join("x" if c is None else str(c) for c in ch), "n_chips": len(ch),
                "n_pad": pad, "n_ofdm_symbols": o.r2d_symbols(len(ch), m)}

    def r2d_duration_tc(self, n_ofdm_symbols, l0):
        return o.r2d_duration_tc(n_ofdm_symbols, l0)

    def r2d_chip_duration_us(self, m):
        return F(1_000_000, (4 if m == "SIP" else m) * 15000)

    def d2r_total_bits(self, e_bits, l_amble, i_bit, i_add):
        return o.d2r_total_bits(e_bits, l_amble, i_bit, i_add)

    def d2r_assemble(self, e_bits, l_amble, i_bit, i_add):
        return o.bstr(o.d2r_assemble(o.bits(e_bits), l_amble, i_bit, i_add))

    def d2r_frame_bits(self, payload_bits, r_block, coded, l_amble, i_bit, i_add):
        B = (payload_bits + o.crc_len(payload_bits)) * r_block
        return o.d2r_total_bits(3 * B if coded else B, l_amble, i_bit, i_add)

    def sched_scale(self, bit_duration_cp):
        return o.S_SCALE[bit_duration_cp]

    def sched_decode(self, bit_duration_cp, interval_cp):
        return {"t_bit_tau": str(o.TBIT_TAU[bit_duration_cp]),
                "i_bit": o.IBIT_BASE[interval_cp] * o.S_SCALE[bit_duration_cp],
                "r_sfs_allowed": o.allowed_rsfs(bit_duration_cp)}

    def sched_freq_bitmap(self, bit_duration_cp, bitmap):
        sel = [o.RSFS_ALL[i] for i, c in enumerate(bitmap) if c == "1"]
        return {"r_sfs_set": sel, "valid": all(x in o.allowed_rsfs(bit_duration_cp) for x in sel)}

    def sched_combo_count(self):
        return sum(len(o.allowed_rsfs(i)) for i in range(8))

    def sched_tchip_set(self):
        return sorted({str(o.tchip_tau(tb, r)) for i, tb in enumerate(o.TBIT_TAU) for r in o.allowed_rsfs(i)},
                      key=lambda s: -F(s))

    def d2r_chip_params(self, t_bit_tau, r_sfs):
        tb = _f(F(t_bit_tau))
        return {"t_chip_tau": str(o.tchip_tau(tb, r_sfs)), "t_chip_us": str(o.tchip_tau(tb, r_sfs) * o.TAU_US),
                "shift_khz": str(F(r_sfs) / (tb * o.TAU_US) * 1000),
                "nominal_bw_khz": str(F(o.nominal_bw_khz(tb)))}

    def delta_code_t1(self, t_chip_tau):
        return o.delta_code_t1(F(t_chip_tau))

    def delta_code_t2(self, n_tbs_bytes):
        return o.delta_code_t2(n_tbs_bytes)

    def t_offset(self, t_chip_tau, m_chip_symb):
        return o.t_offset(F(t_chip_tau), m_chip_symb)

    def t_d2r_min_us(self, t_chip_tau):
        return o.t_d2r_min_us(F(t_chip_tau))

    def t_r2d(self, kind, t_bit_tau, r_sfs_set, r_sfs_own, m_chip_symb, n_tbs_bytes, coded, earlier_occasion,
              d2r_bits=None):
        t = o.t_r2d(kind, F(t_bit_tau), r_sfs_set, r_sfs_own, m_chip_symb, n_tbs_bytes, coded, earlier_occasion,
                    d2r_bits)
        return {"tau": str(t), "us": str(t * o.TAU_US), "te_us": str(t * o.TAU_US / 10)}

    def timing_check(self, t_r2d_us, te_us, measured_us):
        return {"pass": abs(F(measured_us) - F(t_r2d_us)) <= F(te_us)}

    def arfcn_to_mhz(self, n_ref):
        return n_ref * 0.005

    def d2r_cbw_khz(self, nominal_bw_khz, shift_khz):
        tb = next(t for t in o.TBIT_TAU if int(o.nominal_bw_khz(t)) == nominal_bw_khz)
        r = F(str(shift_khz)) * tb * o.TAU_US / 1000
        return o.d2r_cbw_khz(tb, int(r))

    def frc_r2d(self, m_chip_symb, tbs_bits, prb, **_):
        ch, pad = o.r2d_chips(o.bits("01" * (tbs_bits // 2)), m_chip_symb)
        return {"n_pad": pad, "n_chips": len(ch), "n_ofdm_symbols": o.r2d_symbols(len(ch), m_chip_symb),
                "n_rb_min": o.NRB_MIN[m_chip_symb], "prb_ok": prb >= o.NRB_MIN[m_chip_symb]}

    # ---- 38.391 MAC PDUs
    def mac_pack(self, kind, fields):
        return m.pack(kind, fields)

    def mac_parse(self, direction, bits):
        return m.parse(direction, bits)

    def sched_info_unpack(self, carrier, bits):
        s = m.sched_unpack(carrier, bits)
        return {"fields": s, "l1": m.sched_to_l1(carrier, s)}

    def sched_info_length(self, carrier):
        return m.sched_len(carrier)

    def d2r_data_padding_bytes(self, d2r_tbs_bytes, sdu_bytes):
        return d2r_tbs_bytes - 2 - sdu_bytes

    def n_access_occasions(self, field):
        return 2 ** field

    def k_value(self, field):
        return 4 if field else 1

    def frequency_index(self, cp):
        return o.RSFS_ALL[int(cp, 2)]

    def r2d_message_type(self, t):
        return {v: ("paging" if k == "paging" else k) for k, v in m.R2D_TYPE.items()}.get(t, "reserved")

    def d2r_message_type(self, t):
        return "d2r_data" if t == "00" else "reserved"

    def cfa_bitmap_valid(self, bitmap):
        return bitmap.count("1") == 1
