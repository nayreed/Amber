"""Run the JSON vectors in tests/conformance/vectors (or --vectors DIR) against an adapter.

Result per vector: PASS, FAIL, SKIP (adapter lacks the method, or the vector is
data-only), NOTE (spec inconsistency recorded, never fails).
"""
import argparse
import glob
import importlib
import json
import os
import sys
from fractions import Fraction as F

from . import oracle as o

DEFAULT_VECTORS = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "vectors"))


def eq(a, b):
    """Equality with exact fractions for numbers/strings, relative 1e-9 for floats."""
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(eq(x, y) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool) or isinstance(a, dict):
        return a == b
    try:
        fa = F(a) if not isinstance(a, float) else None
        fb = F(b) if not isinstance(b, float) else None
        if fa is not None and fb is not None:
            return fa == fb
        x, y = float(F(a)) if fa is not None else a, float(F(b)) if fb is not None else b
        return abs(x - y) <= 1e-9 * max(1.0, abs(x), abs(y))
    except (ValueError, TypeError, ZeroDivisionError):
        return a == b


def sub(got, exp):
    """every key in exp must match in got (got may carry more)."""
    bad = [k for k in exp if k not in got or not eq(got[k], exp[k])]
    return (not bad), {k: (got.get(k), exp[k]) for k in bad}


def call(adapter, name, *a, **k):
    fn = getattr(adapter, name, None)
    if fn is None:
        raise AttributeError(name)
    return fn(*a, **k)


def run_vector(ad, v):
    k, i, e = v["kind"], v["input"], v["expect"]
    if k == "spec_inconsistency":
        return "NOTE", "; ".join(v.get("note", "").split(". ")[:1])
    if k == "constant":
        got = call(ad, "constant", i["name"])
        return ("PASS" if eq(got, e["value"]) else "FAIL"), f"got {got!r} want {e['value']!r}"
    if k == "m_sequence":
        got = call(ad, "m_sequence", i["length"]); return pf(got == e["bits"], got, e["bits"])
    if k == "crc_parity":
        got = call(ad, "crc_parity", i["bits"], i["L"]); return pf(got == e["parity"], got, e["parity"])
    if k == "crc_attach":
        got = call(ad, "crc_attach", i["bits"]); return pf(got == e["bits"], got, e["bits"])
    if k == "crc_property":
        got = call(ad, "crc_attach", i["bits"])
        L = len(got) - len(i["bits"])
        ok = (len(got) == e["length"] and got.startswith(i["bits"]) and L in (6, 16)
              and o.divisible(o.bits(got), L))
        return pf(ok, f"len {len(got)} L {L}", f"len {e['length']}")
    if k == "line_encode":
        got = call(ad, "line_encode", i["bits"]); return pf(got == e["bits"], got, e["bits"])
    if k == "sfs_modulate":
        got = call(ad, "sfs_modulate", i["bits"], i["r_sfs"], i["mod"]); return pf(list(got) == e["symbols"], got, e["symbols"])
    if k == "r2d_ook_map":
        got = call(ad, "r2d_ook_map", i["chips"]); return pf(list(got) == e["symbols"], got, e["symbols"])
    if k == "tbcc_encode":
        got = call(ad, "tbcc_encode", i["bits"]); return pf(got == e["bits"], got, e["bits"])
    if k == "tbcc_property":
        c = i["bits"]; n = len(c); base = call(ad, "tbcc_encode", c)
        rot = c[3:] + c[:3]
        r = call(ad, "tbcc_encode", rot)
        return pf(len(base) == 3 * n and r == base[9:] + base[:9], "rotation/length property", "holds")
    if k == "nrb_min":
        got = call(ad, "nrb_min", i["m_chip_symb"]); return pf(got == e["n_rb_min"], got, e["n_rb_min"])
    if k == "r2d_chip_layout":
        got = call(ad, "r2d_chip_layout", i["tb_bits"], i["m_chip_symb"])
        if "chips" in e:   # x = free padding value; compare all other positions
            c, w = got["chips"], e["chips"]
            ok = len(c) == len(w) and all(a == b or "x" in (a, b) for a, b in zip(c, w))
            ok2, bad = sub(got, {x: y for x, y in e.items() if x != "chips"})
            return pf(ok and ok2, bad or "chip string differs", "match")
    if k == "r2d_duration":
        got = call(ad, "r2d_duration_tc", i["n_ofdm_symbols"], i["l0"]); return pf(got == e["duration_tc"], got, e["duration_tc"])
    if k == "r2d_chip_duration":
        got = call(ad, "r2d_chip_duration_us", i["m_chip_symb"]); return pf(eq(got, e["us"]), got, e["us"])
    if k == "d2r_total_bits":
        got = call(ad, "d2r_total_bits", i["e_bits"], i["l_amble"], i["i_bit"], i["i_add"]); return pf(got == e["v"], got, e["v"])
    if k == "d2r_assemble":
        got = call(ad, "d2r_assemble", i["e_bits"], i["l_amble"], i["i_bit"], i["i_add"]); return pf(got == e["bits"], got, e["bits"])
    if k == "d2r_frame_bits":
        got = call(ad, "d2r_frame_bits", i["payload_bits"], i["r_block"], i["coded"], i["l_amble"], i["i_bit"], i["i_add"])
        ok = got == e["v"] and ("duration_us" not in e or eq(F(got) * 2 * o.TAU_US, e["duration_us"]))
        return pf(ok, got, e["v"])
    if k == "frc_d2r_total_bits":
        got = call(ad, "d2r_frame_bits", i["payload_bits"], i["r_block"], i["fec"] != "none", i["l_amble"],
                   i["i_bit_effective"], i["i_add"])
        return pf(got == e["total_symbols"], got, e["total_symbols"])
    if k == "sched_scale":
        got = call(ad, "sched_scale", i["bit_duration_cp"]); return pf(got == e["s"], got, e["s"])
    if k == "sched_decode":
        got = call(ad, "sched_decode", i["bit_duration_cp"], i["interval_cp"]); ok, bad = sub(got, e); return pf(ok, bad, "match")
    if k == "sched_freq_bitmap":
        got = call(ad, "sched_freq_bitmap", i["bit_duration_cp"], i["bitmap"]); ok, bad = sub(got, e); return pf(ok, bad, "match")
    if k == "sched_combo_count":
        got = call(ad, "sched_combo_count"); return pf(got == e["allowed_pairs"], got, e["allowed_pairs"])
    if k == "sched_tchip_set":
        got = call(ad, "sched_tchip_set"); return pf(eq(list(got), e["t_chip_tau"]), got, e["t_chip_tau"])
    if k == "d2r_chip_params":
        got = call(ad, "d2r_chip_params", i["t_bit_tau"], i["r_sfs"]); ok, bad = sub(got, e); return pf(ok, bad, "match")
    if k == "delta_code_t1":
        got = call(ad, "delta_code_t1", i["t_chip_tau"]); return pf(eq(got, e["delta_code_tau"]), got, e["delta_code_tau"])
    if k == "delta_code_t2":
        got = call(ad, "delta_code_t2", i["n_tbs_bytes"]); return pf(eq(got, e["delta_code_tau"]), got, e["delta_code_tau"])
    if k == "t_offset":
        got = call(ad, "t_offset", i["t_chip_tau"], i["m_chip_symb"]); return pf(eq(got, e["t_offset_tau"]), got, e["t_offset_tau"])
    if k == "t_d2r_min":
        got = call(ad, "t_d2r_min_us", i["t_chip_tau"]); return pf(eq(got, e["us"]), got, e["us"])
    if k == "t_r2d":
        got = call(ad, "t_r2d", **i); ok, bad = sub(got, e); return pf(ok, bad, "match")
    if k == "timing_check":
        got = call(ad, "timing_check", i["t_r2d_us"], i["te_us"], i["measured_us"]); return pf(got["pass"] == e["pass"], got, e)
    if k == "arfcn_to_mhz":
        got = call(ad, "arfcn_to_mhz", i["n_ref"]); return pf(eq(got, e["mhz"]), got, e["mhz"])
    if k == "d2r_cbw":
        got = call(ad, "d2r_cbw_khz", i["nominal_bw_khz"], i["shift_khz"]); return pf(got == e["cbw_khz"], got, e["cbw_khz"])
    if k == "frc_r2d" and "n_pad" in e:
        got = call(ad, "frc_r2d", i["m_chip_symb"], i["tbs_bits"], i["prb"])
        exp = {x: e[x] for x in ("n_pad", "n_chips", "n_ofdm_symbols", "n_rb_min", "prb_ok")}
        ok, bad = sub(got, exp); return pf(ok, bad, "match")
    if k == "mac_pack":
        got = call(ad, "mac_pack", i["kind"], i["fields"])
        n = e.get("fill_bits", 0)
        if n and len(got) == len(e["bits"]):      # fill content is free: compare all but the fill
            return pf(got[:-n] == e["bits"][:-n], got, e["bits"])
        return pf(got == e["bits"], got, e["bits"])
    if k == "mac_parse":
        kind, fields = call(ad, "mac_parse", i["direction"], i["bits"])
        fb = fields.pop("fill_bits", None) if isinstance(fields, dict) else None
        fields = dict(fields)
        want = dict(e["fields"])
        ok = kind == e["kind"] and fields == want and (fb is None or "fill_bits" not in e or fb == e["fill_bits"])
        return pf(ok, (kind, fields, fb), (e["kind"], want, e.get("fill_bits")))
    if k == "sched_info_unpack":
        got = call(ad, "sched_info_unpack", i["carrier"], i["bits"])
        ok = got.get("fields") == e["fields"] and sub(got.get("l1", {}), e["l1"])[0]
        return pf(ok, got, e)
    if k == "sched_info_length":
        got = call(ad, "sched_info_length", i["carrier"]); return pf(got == e["bits"], got, e["bits"])
    if k == "d2r_data_tb_relation":
        got = call(ad, "d2r_data_padding_bytes", i["d2r_tbs_bytes"], i["sdu_bytes"]); return pf(got == e["padding_bytes"], got, e["padding_bytes"])
    if k == "cfa_bitmap_valid":
        got = call(ad, "cfa_bitmap_valid", i["bitmap"]); return pf(got == e["valid"], got, e["valid"])
    if k == "n_access_occasions":
        got = call(ad, "n_access_occasions", i["field"]); return pf(got == e["occasions"], got, e["occasions"])
    if k == "k_value":
        got = call(ad, "k_value", i["field"]); return pf(got == e["k"], got, e["k"])
    if k == "frequency_index":
        got = call(ad, "frequency_index", i["cp"]); return pf(got == e["r_sfs"], got, e["r_sfs"])
    if k == "r2d_message_type":
        got = call(ad, "r2d_message_type", i["type"]); return pf(got == e["name"], got, e["name"])
    if k == "d2r_message_type":
        got = call(ad, "d2r_message_type", i["type"]); return pf(got == e["name"], got, e["name"])
    if k == "limit":
        got = call(ad, "limit", i["name"]); return pf(eq(got, e["value"]), got, e["value"])
    return "SKIP", "data-only vector (no executable check)"


def pf(ok, got, want):
    return ("PASS", "") if ok else ("FAIL", f"got {got!r} want {want!r}")


def run(adapter, vec_dir=DEFAULT_VECTORS, only=None, verbose=False):
    tot = {"PASS": 0, "FAIL": 0, "SKIP": 0, "NOTE": 0}
    rows = []
    for path in sorted(glob.glob(os.path.join(vec_dir, "*.json"))):
        doc = json.load(open(path))
        if "vectors" not in doc or (only and os.path.basename(path) not in only):
            continue
        for v in doc["vectors"]:
            try:
                st, msg = run_vector(adapter, v)
            except (AttributeError, NotImplementedError) as ex:
                st, msg = "SKIP", f"adapter has no {ex}"
            tot[st] += 1
            rows.append((st, v["id"], msg, v.get("origin"), v.get("cite", [""])[0]))
            if st in ("FAIL", "NOTE") or verbose:
                print(f"{st:4} {v['id']:30} {msg}   [{v.get('origin')}; {v.get('cite', [''])[0]}]")
    return tot, rows


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aiot_conformance")
    ap.add_argument("--vectors", default=DEFAULT_VECTORS)
    ap.add_argument("--impl", help="module:Class providing the adapter methods (default: reference oracle)")
    ap.add_argument("--self-check", action="store_true", help="run the oracle against its own vectors")
    ap.add_argument("--only", nargs="*", help="vector file names")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    if a.impl:
        m, c = a.impl.split(":")
        adapter = getattr(importlib.import_module(m), c)()
    else:
        from .oracle_adapter import OracleAdapter
        adapter = OracleAdapter()
    tot, rows = run(adapter, a.vectors, a.only, a.verbose)
    print(tot)
    return 1 if tot["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
