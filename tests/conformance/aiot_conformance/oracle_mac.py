"""38.391 V19.4.0 §6 MAC PDU reference packer/parser, written from the field tables and figures.

Bits are strings, first (most significant) bit first, fields concatenated in reading order of the
figures (§6.1.1). Raw field values are used throughout: codepoints, not decoded meanings, except
r2d_tbs, which is the TBS in bytes; its field holds codepoint = bytes - 1 (§6.1.1, the first codepoint
is the first value), as the D2R TBS does.
Fill bits and padding are packed as zeros; their values are ignored on parse (§6.2.1.1, CR 0016).
A paging has an SPPI bit after the TBS and, when it is 1, a 128-bit Security Parameter (§6.2.1.1,
5.2); the Paging ID Length is 10 bits.
"""
from fractions import Fraction as F
from . import oracle as o

R2D_TYPE = {"paging": "001", "access_trigger": "010", "random_id_response": "011",
            "r2d_data": "100", "nack_feedback": "101"}          # Table 6.1.2-1
D2R_TYPE_DATA = "00"                                             # Table 6.1.2-2

# child fields of D2R Scheduling Info in Table 6.2.1.6-1 row order, per carrying message
SCHED_FIELDS = {
    "paging_cbra": [("time_resource_cp", 1), ("bit_duration_cp", 3), ("freq_bitmap", 8), ("block_rep_cp", 1),
                    ("coding_cp", 1), ("interval_cp", 2), ("seq_len_cp", 1), ("add_midamble_cp", 1)],
    "paging_cfa": [("bit_duration_cp", 3), ("freq_bitmap", 8), ("block_rep_cp", 1), ("coding_cp", 1),
                   ("interval_cp", 2), ("seq_len_cp", 1), ("add_midamble_cp", 1), ("d2r_tbs_cp", 7)],
    "random_id_response": None,   # same layout as paging_cfa
    "r2d_data": [("bit_duration_cp", 3), ("freq_unicast_cp", 3), ("block_rep_cp", 1), ("coding_cp", 1),
                 ("interval_cp", 2), ("seq_len_cp", 1), ("add_midamble_cp", 1), ("d2r_tbs_cp", 7)],
}
SCHED_FIELDS["random_id_response"] = SCHED_FIELDS["paging_cfa"]


SEC_BITS = 128           # Security Parameter: RAND_AIOT_n of TS 33.369
PAGING_ID_LEN_BITS = 10  # Paging ID Length (V19.4.0)


def b(v, n):
    assert 0 <= v < (1 << n), (v, n)
    return format(v, f"0{n}b")


def sched_pack(carrier, s):
    out = ""
    for name, n in SCHED_FIELDS[carrier]:
        out += s[name] if name == "freq_bitmap" else b(s[name], n)
    return out


def sched_unpack(carrier, bits):
    s, p = {}, 0
    for name, n in SCHED_FIELDS[carrier]:
        s[name] = bits[p:p + n] if name == "freq_bitmap" else int(bits[p:p + n], 2)
        p += n
    assert p == len(bits)
    return s


def sched_len(carrier):
    return sum(n for _, n in SCHED_FIELDS[carrier])


def sec_pack(f):
    """SPPI and, when it is 1, the Security Parameter (hex string of 128 bits)."""
    if not f["sppi"]:
        return [("sppi", "0")]
    return [("sppi", "1"), ("security_parameter", b(int(f["security_parameter"], 16), SEC_BITS))]


def sched_to_l1(carrier, s):
    """Decode raw codepoints to the L1 parameters of 38.291 §7.1.1 (enumeration rule of §6.2.1.6)."""
    bd = s["bit_duration_cp"]
    r = {"t_bit_tau": str(o.TBIT_TAU[bd]), "r_block": s["block_rep_cp"] + 1, "coded": s["coding_cp"] == 0,
         "i_bit": o.IBIT_BASE[s["interval_cp"]] * o.S_SCALE[bd], "l_amble": 31 if s["seq_len_cp"] else 7,
         "i_add": bool(s["add_midamble_cp"])}
    if "time_resource_cp" in s:
        r["x"] = s["time_resource_cp"] + 1
    if "freq_bitmap" in s:
        r["r_sfs_set"] = [o.RSFS_ALL[i] for i, c in enumerate(s["freq_bitmap"]) if c == "1"]
        r["r_sfs_valid"] = all(x in o.allowed_rsfs(bd) for x in r["r_sfs_set"])
    if "freq_unicast_cp" in s:
        r["r_sfs"] = o.RSFS_ALL[s["freq_unicast_cp"]]
        r["r_sfs_valid"] = r["r_sfs"] in o.allowed_rsfs(bd)    # Table 6.2.1.6-1 (CR 0009): as the Broadcast values
    if "d2r_tbs_cp" in s:
        r["d2r_tbs_bytes"] = s["d2r_tbs_cp"] + 1
    return r


def _bytealign(bits):
    return bits + "0" * (-len(bits) % 8)


def segs_pack(kind, f):
    """Return list of (name, bits) segments for a R2D/D2R message. r2d_tbs must be given."""
    T = lambda k: R2D_TYPE[k]
    if kind == "paging_cbra":
        sg = [("type", T("paging")), ("r2d_tbs", b(f["r2d_tbs"] - 1, 7))] + sec_pack(f)
        sg += [("at", "1"), ("transaction_id", b(f["transaction_id"], 6))]
        pid = f.get("paging_id")
        sg.append(("pipi", "1" if pid is not None else "0"))
        if pid is not None:
            sg += [("paging_id_length", b(len(pid), PAGING_ID_LEN_BITS)), ("paging_id", pid)]
        sg += [("n_access_occasions_exp", b(f["n_access_occasions_exp"], 4)),
               ("d2r_sched_info", sched_pack("paging_cbra", f["sched"])), ("k", b(f["k_cp"], 1))]
    elif kind == "paging_cfa":
        pid = f["paging_id"]
        sg = [("type", T("paging")), ("r2d_tbs", b(f["r2d_tbs"] - 1, 7))] + sec_pack(f)
        sg += [("at", "0"), ("paging_id_length", b(len(pid), PAGING_ID_LEN_BITS)), ("paging_id", pid),
               ("d2r_sched_info", sched_pack("paging_cfa", f["sched"]))]
    elif kind == "access_trigger":
        return [("type", T("access_trigger"))]
    elif kind == "random_id_response":
        fipi = f["fipi"]
        sg = [("type", T("random_id_response")), ("r2d_tbs", b(f["r2d_tbs"] - 1, 7)),
              ("d2r_sched_info", sched_pack("random_id_response", f["sched"])), ("fipi", str(fipi))]
        for i, e in enumerate(f["entries"], 1):
            if fipi:
                sg.append((f"frequency_index_{i}", b(e["frequency_index_cp"], 3)))
            sg += [(f"echoed_random_id_{i}", b(e["echoed_random_id"], 16)),
                   (f"aipi_{i}", "1" if e.get("assigned_as_id") is not None else "0")]
            if e.get("assigned_as_id") is not None:
                sg.append((f"assigned_as_id_{i}", b(e["assigned_as_id"], 16)))
    elif kind == "r2d_data":
        sg = [("type", T("r2d_data")), ("r2d_tbs", b(f["r2d_tbs"] - 1, 7)), ("as_id", b(f["as_id"], 16)),
              ("d2r_sched_info", sched_pack("r2d_data", f["sched"]))]
        if f["ci"] == 1:
            sg += [("ci", "1"), ("r", "00"), ("data_sdu", "".join(b(x, 8) for x in bytes.fromhex(f["data_sdu_hex"])))]
        else:
            sg += [("ci", "0"), ("r", "000"), ("received_data_size", b(f["received_data_size"], 7))]
    elif kind == "nack_feedback":
        sg = [("type", T("nack_feedback")), ("r2d_tbs", b(f["r2d_tbs"] - 1, 7)), ("r", "000000")]
        sg += [(f"as_id_{i}", b(a, 16)) for i, a in enumerate(f["as_ids"], 1)]
    elif kind == "access_random_id":
        return [("random_id", b(f["random_id"], 16))]
    elif kind == "d2r_data":
        sdu = bytes.fromhex(f["sdu_hex"])
        sg = [("type", D2R_TYPE_DATA), ("r", "000000"), ("mdi", b(f["mdi"], 1)), ("sdu_length", b(len(sdu), 7)),
              ("data_sdu", "".join(b(x, 8) for x in sdu))]
        pad = f.get("padding_bytes", 0)
        if pad:
            sg.append(("padding", "0" * (8 * pad)))
        return sg
    else:
        raise ValueError(kind)
    n = sum(len(x) for _, x in sg)
    if -n % 8:
        sg.append(("fill_bits", "0" * (-n % 8)))
    return sg


def pack(kind, f):
    return "".join(x for _, x in segs_pack(kind, f))


def parse(direction, bits):
    """Inverse of pack. Returns (kind, fields) with fields as accepted by pack plus 'fill_bits' (count)."""
    p = 0

    def take(n):
        nonlocal p
        v = bits[p:p + n]
        assert len(v) == n, "PDU too short"
        p += n
        return v

    if direction == "d2r":
        if len(bits) == 16:
            return "access_random_id", {"random_id": int(bits, 2)}
        assert take(2) == D2R_TYPE_DATA
        take(6)
        mdi = int(take(1)); ln = int(take(7), 2)
        sdu = take(8 * ln)
        return "d2r_data", {"mdi": mdi, "sdu_hex": "%0*X" % (2 * ln, int(sdu, 2)) if ln else "",
                            "padding_bytes": (len(bits) - p) // 8}
    t = take(3)
    inv = {v: k for k, v in R2D_TYPE.items()}
    k = inv[t]
    if k == "access_trigger":
        return k, {}
    tbs = int(take(7), 2) + 1                                    # R2D TBS: codepoint + 1 bytes
    if k == "paging":
        sec = {"sppi": int(take(1))}
        if sec["sppi"]:
            sec["security_parameter"] = "%0*X" % (SEC_BITS // 4, int(take(SEC_BITS), 2))
        at = take(1)
        if at == "1":
            tid = int(take(6), 2)
            pid = None
            if take(1) == "1":
                pid = take(int(take(PAGING_ID_LEN_BITS), 2))
            n = int(take(4), 2)
            sched = sched_unpack("paging_cbra", take(18))
            kc = int(take(1))
            return "paging_cbra", {"r2d_tbs": tbs, **sec, "transaction_id": tid, "paging_id": pid,
                                   "n_access_occasions_exp": n, "sched": sched, "k_cp": kc,
                                   "fill_bits": len(bits) - p}
        pid = take(int(take(PAGING_ID_LEN_BITS), 2))
        sched = sched_unpack("paging_cfa", take(24))
        return "paging_cfa", {"r2d_tbs": tbs, **sec, "paging_id": pid, "sched": sched, "fill_bits": len(bits) - p}
    if k == "random_id_response":
        sched = sched_unpack("random_id_response", take(24))
        fipi = int(take(1))
        ents = []
        while len(bits) - p >= 17 + (3 if fipi else 0):      # fewer left than one entry = fill bits
            e = {}
            if fipi:
                e["frequency_index_cp"] = int(take(3), 2)
            e["echoed_random_id"] = int(take(16), 2)
            e["assigned_as_id"] = int(take(16), 2) if take(1) == "1" else None
            ents.append(e)
        return k, {"r2d_tbs": tbs, "sched": sched, "fipi": fipi, "entries": ents, "fill_bits": len(bits) - p}
    if k == "r2d_data":
        asid = int(take(16), 2)
        sched = sched_unpack("r2d_data", take(19))
        ci = int(take(1))
        f = {"r2d_tbs": tbs, "as_id": asid, "sched": sched, "ci": ci}
        if ci:
            take(2)
            f["data_sdu_hex"] = bits[p:].encode() and "%0*X" % ((len(bits) - p) // 4, int(bits[p:], 2)) if len(bits) > p else ""
        else:
            take(3)
            f["received_data_size"] = int(take(7), 2)
        return k, f
    if k == "nack_feedback":
        take(6)
        ids = []
        while len(bits) - p >= 16:
            ids.append(int(take(16), 2))
        return k, {"r2d_tbs": tbs, "as_ids": ids}
    raise ValueError(k)
