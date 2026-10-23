"""Reference model written directly from the Rel-19 A-IoT spec text.

It is the oracle for the vector files: the expected values it derives are
the ones the vectors hold, and `python -m aiot_conformance --self-check` runs
every vector back through it. It shares no code with amber.phy or amber.mac.
Clause numbers refer to TS 38.291 V19.3.0 unless stated.
"""
from fractions import Fraction as F
from math import ceil, floor

KAPPA = 64                                   # 4.3.1
TAU_US = F(2_000_000, 15000)                 # 4.2: tau = 2e6/15000 microseconds
TC_US = F(1_000_000, 480_000 * 4096)         # 4.3.1: Tc = 1/(480e3*4096) s
N_U = 2048 * KAPPA                           # 6.2.7
N_CP_L0 = 160 * KAPPA                        # 6.2.7, OFDM symbols with l mod 7 == l0
N_CP_OTHER = 144 * KAPPA

SIP = [1, 1, 0, 0, 1, 0, 0, 0]               # 6.2.3.1
CAP = [1, 0, 1, 0]                           # 6.2.3.2
POSTAMBLE = [1, 1, 1, 1]                     # 6.2.4
N_SIP, N_CAP = 8, 4

NRB_MIN = {2: 1, 6: 1, 12: 2, 24: 3}         # Table 4.3.3.3-1


def bits(s):
    return [int(c) for c in s]


def bstr(b):
    return "".join(str(int(x)) for x in b)


# ---- 8.1 CRC ---------------------------------------------------------
GEN = {16: (1 << 16) | (1 << 12) | (1 << 5) | 1, 6: (1 << 6) | (1 << 5) | 1}


def crc_len(a):
    """6.1.2.1 / 6.2.2.1: L=16 if A>24, else 6."""
    return 16 if a > 24 else 6


def crc_parity(a_bits, L):
    """Parity pi_0..pi_{L-1}: remainder of a(D)*D^L / g(D), pi_0 = D^(L-1) coeff."""
    g = GEN[L]
    reg = 0
    for x in a_bits:
        reg = (reg << 1) | x
        if reg >> L & 1:
            reg ^= g
    for _ in range(L):
        reg <<= 1
        if reg >> L & 1:
            reg ^= g
    return [(reg >> (L - 1 - i)) & 1 for i in range(L)]


def crc_attach(a_bits):
    return list(a_bits) + crc_parity(a_bits, crc_len(len(a_bits)))


def divisible(b_bits, L):
    """8.1: the codeword polynomial leaves remainder 0 modulo g."""
    g, reg = GEN[L], 0
    for x in b_bits:
        reg = (reg << 1) | x
        if reg >> L & 1:
            reg ^= g
    return reg == 0


# ---- 8.3 sequences ---------------------------------------------------
def m_sequence(n):
    if n == 7:
        x = [1, 0, 0]
        while len(x) < 7:
            m = len(x) - 3
            x.append(x[m + 2] ^ x[m])
    elif n == 31:
        x = [0, 1, 0, 0, 1]
        while len(x) < 31:
            m = len(x) - 5
            x.append(x[m + 3] ^ x[m])
    else:
        raise ValueError(n)
    return x


# ---- 8.4 / 8.5 mapping -----------------------------------------------
def line_encode(v):
    out = []
    for b in v:
        out += [(2 * j - 1) * b + (1 - j) for j in (0, 1)]
    return out


def sfs_modulate(v, r_sfs, mod):
    out = []
    for b in v:
        sym = [0] * (2 * r_sfs)
        for k in range(r_sfs):
            for j in (0, 1):
                sym[j + 2 * k] = ((2 * j - 1) * b + (1 - j) if mod == "OOK"
                                  else 2 * (2 * j - 1) * b + (1 - 2 * j))
        out += sym
    return out


# ---- 8.2 tail-biting convolutional code ------------------------------
G_OCT = (0o133, 0o171, 0o165)


def tbcc_encode(c):
    """Returns e_0..e_{3K-1}, e_{3k+i} = d_k^(i) (6.1.2.3). Window index 0 is
    c_k, index n is c_{k-n} (circular, per the 8.2 initial state), and bit
    (6-n) of the octal generator weights index n (Figure 8.2-1 taps)."""
    K = len(c)
    e = []
    for k in range(K):
        w = [c[(k - n) % K] for n in range(7)]
        for g in G_OCT:
            e.append(sum(w[n] for n in range(7) if (g >> (6 - n)) & 1) & 1)
    return e


# ---- 6.1.3 amble insertion -------------------------------------------
def d2r_total_bits(E, l_amble, i_bit, i_add):
    n = ceil(E / i_bit) if i_add else floor(E / i_bit)
    return E + (1 + n) * l_amble


def d2r_assemble(e, l_amble, i_bit, i_add):
    E = len(e)
    V = d2r_total_bits(E, l_amble, i_bit, i_add)
    m = m_sequence(l_amble)
    v = [None] * V
    for k in range(l_amble):
        v[k] = m[k]
    if E >= i_bit:
        for K in range(1, E // i_bit + 1):
            for k in range(l_amble):
                v[k + K * (l_amble + i_bit)] = m[k]
    if i_add:
        for k in range(l_amble):
            v[k + V - l_amble] = m[k]
    it = iter(e)
    return [next(it) if x is None else x for x in v]


# ---- 6.2.5 R2D chip mapping ------------------------------------------
def r2d_chips(tb_bits, M):
    """Chips chi=0.. (SIP, CAP, PRDCH, postamble, padding). Free padding
    chips ('any values') are None. Returns (chips, n_pad)."""
    stream = line_encode(crc_attach(tb_bits)) + POSTAMBLE
    chips = list(SIP) + list(CAP)
    it = iter(stream)
    pending = len(stream)
    while pending:
        pos = (len(chips) - N_SIP) % M
        if M == 24 and pos >= M - 2:
            chips.append(1)
        else:
            chips.append(next(it))
            pending -= 1
    n_pad = 0
    while (len(chips) - N_SIP) % M:
        chips.append(None)
        n_pad += 1
    if M == 24:
        # final two padding chips are 1; if pad is only the forced pair it is still 2
        if n_pad == 0:
            chips += [1, 1]
            n_pad = 2
        else:
            chips[-2:] = [1, 1]
    return chips, n_pad


def r2d_symbols(n_chips, M):
    return N_SIP // 4 + (n_chips - N_SIP) // M


def r2d_symbol_tc(l, l0):
    return N_U + (N_CP_L0 if l % 7 == l0 else N_CP_OTHER)


def r2d_duration_tc(n_sym, l0):
    return sum(r2d_symbol_tc(l, l0) for l in range(n_sym))


# ---- 38.391 Table 6.2.1.6-1 ------------------------------------------
TBIT_TAU = [F(2), F(1), F(1, 2), F(1, 4), F(1, 8), F(1, 16), F(1, 32), F(1, 96)]
RSFS_ALL = [1, 2, 4, 8, 16, 32, 64, 128]
S_SCALE = [1, 2, 4, 8, 16, 32, 64, 192]
IBIT_BASE = [48, 96, 168, 240]


def allowed_rsfs(tbit_idx):
    return RSFS_ALL[: 8 - tbit_idx]


def tchip_tau(tbit_tau, r):
    return tbit_tau / (2 * r)


# ---- 7.1.2 timing (units of tau) -------------------------------------
def delta_code_t1(tchip):
    return F(0) if tchip >= F(1, 16) else F(1)       # Table 7.1.2-1


def delta_code_t2(n_tbs):
    return F(2) if n_tbs <= 32 else F(4) if n_tbs <= 64 else F(8)   # 7.1.2-2


def t_offset(tchip, M):
    if tchip >= F(1, 2):
        return F(10)
    if tchip >= F(1, 16):
        return F(5)
    if tchip == F(1, 32):
        return F(1)
    return F(1) if M == 2 else F(1, 4)               # tau/64, /128, /192


def t_r2d(kind, tbit, r_set, r_own, M, n_tbs, coded, earlier, n_bits_d2r=None):
    """kind: msg1 | msg3_after_msg2 | cfa | other. Returns T_R->D in tau."""
    tchip_own = tchip_tau(tbit, r_own)
    if kind in ("msg1", "msg2resp"):
        tchip = max(tchip_tau(tbit, r) for r in r_set)
    else:
        tchip = tchip_own
    off = t_offset(tchip, M)
    if kind == "msg1":
        if earlier:
            return F(5, 4) * (off + n_bits_d2r * tbit)   # M_chip*T_chip = V*T_bit
        return off
    if n_tbs <= 32 and kind in ("msg2resp", "cfa"):
        return off + (delta_code_t1(tchip) if coded else 0)
    return off + (delta_code_t2(n_tbs) if coded else 0)


def t_d2r_min_us(tchip_tau_):
    return max(3 * tchip_tau_ * TAU_US, F(10))


# ---- 38.191 Annex E ---------------------------------------------------
def d2r_cbw_khz(tbit_tau, r, sfo=F(1, 10)):
    tb_us = tbit_tau * TAU_US
    return ceil((2 + 2 * r) / tb_us * 1000 * (1 + sfo))


def nominal_bw_khz(tbit_tau):
    return 4 / (tbit_tau * TAU_US) * 1000
