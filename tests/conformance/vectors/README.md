# A-IoT Rel-19 PHY conformance vectors

Language-neutral JSON, derived only from the spec text. Run them with `python3 -m aiot_conformance`
(see `tests/conformance/README.md`). A C test can read the same files directly.

Specs used: TS 38.291 V19.3.0, TS 38.391 V19.4.0, TS 38.191 V19.4.0, TS 38.194 V19.3.0.

## File layout

Each file is `{file, title, schema, specs, vectors:[...]}`. A vector is

    {"id", "kind", "origin", "cite":[...], "input":{...}, "expect":{...}, "note"?, "xcheck"?}

`index.json` lists the files and vector counts.

## origin: how much to trust a vector

| origin | meaning |
|---|---|
| `spec-literal` | The value, table cell or bit string is printed in the spec. Typed by hand, then checked against the page. |
| `spec-derived` | Computed from a formula or definition the spec states (CRC polynomial, §6.1.3 length formula, Tables 7.1.2-x and so on) by `oracle.py`. No reading choice involved. |
| `spec-inferred` | Needs a reading the spec does not spell out. The `note` says what was assumed. Treat a failure here as "check the reading", not "implementation wrong". |

Every vector carries `cite` with spec version and clause or table, and, except for TS 38.391, the printed page number.

## Files

| file | content |
|---|---|
| `sequences.json` | SIP/CAP/postamble, m-sequences 7 and 31, CRC and TBCC constants, κ, τ, Tc |
| `crc.json` | CRC6/CRC16 parity for 8 payloads (both polynomials), the A>24 rule, remainder-zero property |
| `modulation.json` | Manchester line code, OOK/BPSK small-frequency-shift mapping, R2D OOK |
| `tbcc.json` | Tail-biting rate-1/3 K=7 encoder vectors and a rotation property |
| `r2d.json` | N_RB^min, full R2D chip layouts (SIP, CAP, PRDCH, postamble, padding, M=24 forced chips), OFDM symbol durations for all l0, chip durations, the Annex B.2 padding inconsistency |
| `d2r_frame.json` | §6.1.3 total bits V, amble placement, FRC total symbols (584, 143), 9A Msg1 (128 bits) and Msg3 (584 bits) |
| `sched_info.json` | 38.391 Table 6.2.1.6-1: field widths, bit-duration codepoints, S scale, I_bit, R_SFS gating, T_chip values |
| `timing.json` | 38.291 Tables 7.1.2-1/2/3, T_R→D cases, Te, T_D2R_min, the pass/fail rule of 38.191 §9A.2.3 |
| `rf_limits.json` | REFSENS, EIS, max input, backscatter loss, BS REFSENS, emissions, bands, guard bands, channel raster, D2R channel bandwidth table |
| `mac_pdu.json` | 38.391 §6 (V19.4.0): bit strings for every R2D message (Paging CBRA/CFA with SPPI 0 and 1, Access Trigger, Random ID Response, R2D Upper Layer Data CI=0/1, NACK Feedback) and D2R message (Msg1, data), with per-field segments; D2R Scheduling Info decoded to L1 parameters per carrying message; message-type tables; TBS/padding relations |
| `frc_r2d.json` | R2D reference channels (38.194 B.2, 38.191 B.2.1/B.2.2/B.3.1) |

## OAI's values

`crc_parity` and `tbcc_encode` vectors for byte-aligned inputs also carry `xcheck`: the value OAI's `crc6`, `crc16` and
`ccodelte_encode` give for the same input, which equals the expected value.

## Readings you need to know about (also in the `note` of the vectors)

1. **38.194 Annex B.2, M=24 padding.** Printed 8, §6.2.5 gives 12 (8 ignores the forced "1" chips). M=6 and M=12 agree. Recorded as a `NOTE`, never pass/fail.
2. **A-FR1-A1-3/-4 total 143.** Reproduced only with I_bit = 48 × S, S = 192 at T_bit = τ/96. Without S it would be 205. The bit duration itself is inferred from the nominal bandwidth (2880 kHz = 4/T_bit).
3. **9A Msg1 CRC.** Table 9A.2.2-3 says CRC 16. Msg1 is a 16-bit Random ID, so §6.1.2.1 gives CRC6 and a 128-bit frame (34.13 ms), which fits T2 = 35 ms. CRC16 would be 189 bits (50.4 ms) and would not fit.
4. **9A Msg3 T_R→D** assumes Msg2 offers R_SFS {8}; the spec text for 9A does not give Msg2's frequency indication.
5. **Te.** Taken as 10% of T_R→D (T_SFO measured to the end of the last R2D chip, 38.291 §7.2.4).
6. **l0** (the OFDM symbol index carrying the long CP) is a free choice in 38.291 §6.2.7, so durations are given for all seven.
7. **Extra midamble** coincides with the final regular midamble when E is a multiple of I_bit (the 584-bit FRC has that shape but no extra midamble).
8. **Channel bandwidth table** is reproduced by the Annex E formula only with |SFO| = 0.1, which the spec does not state for the table.
9. **TBCC** tap order is read from Figure 8.2-1 (the text gives only the octal generators). OAI's LTE encoder agrees.
10. **Not covered:** how the device maps the received CAP to M_chip (38.291 §7.2.1 refers to Table 4.3.3.3-1, which has no CAP rate column).

## MAC PDU vectors (mac_pdu.json): adapter methods and readings

Adapter: `mac_pack(kind, fields) -> bitstring`, `mac_parse(direction, bits) -> (kind, fields)` (may include `fill_bits` = count),
`sched_info_unpack(carrier, bits) -> {fields, l1}`, `sched_info_length(carrier)`, `d2r_data_padding_bytes(tbs, sdu)`,
`n_access_occasions`, `k_value`, `frequency_index`, `r2d_message_type`, `d2r_message_type`. Fields are raw values (codepoints);
kinds: paging_cbra, paging_cfa, access_trigger, random_id_response, r2d_data, nack_feedback, access_random_id, d2r_data. Each pack vector has
`segments` (field name, bits) to diff against. Bit strings are first-bit-first, fields in figure reading order.

11. **R2D TBS encoding is ambiguous.** The scheduling-info text says the first codepoint is the first value, so D2R TBS bytes = codepoint + 1. The R2D TBS field of the message header only says "the value can be {1..125}". Vectors use the direct binary value = PDU length in bytes (also used as N_TBS^R2D by 38.291 §7.2.2). If it is offset by one, only the 7 TBS bits change. Recorded as a NOTE.
12. **Child-field order inside D2R Scheduling Info** is taken from the row order of Table 6.2.1.6-1; the widths then add up to the stated 18 (CBRA paging), 24 (CFA paging, Msg2) and 19 (R2D data) bits, which supports it.
13. **Fill bits and padding** content is unspecified ("ignored"); vectors write zeros and the runner ignores the fill bits when comparing a packed PDU. Parse compares only the fill count.
14. **Access Trigger** is 3 bits (`010`), not byte aligned, no TBS field.
15. **Fill Bits are ignored on parse** (§6.2.1.1, CR 0016): a packed PDU has zeros there, but a parser takes any value. `mac_parse-020` is a Paging with every Fill Bit set to 1.
16. **SPPI and Security Parameter** (§6.2.1.1, 5.2): a Paging has an SPPI bit after the TBS. With SPPI 1 the 128-bit Security Parameter (RAND_AIOT_n of TS 33.369) follows it; with SPPI 0 there is none. Fields: `sppi` (0 or 1) and `security_parameter` (32 hex digits, only with SPPI 1); segments `sppi`, `security_parameter`. A Reader sends SPPI 1 ("set to 1 in this release"), a device accepts both.
17. **Paging ID Length is 10 bits** (V19.4.0; it was 8). A Paging ID has up to 1023 bits, and 125 bytes hold at most 954 (CFA, SPPI 0). `mac_parse-021` and `-022` have a 300-bit Paging ID.
18. **Unicast R_SFS is gated by the Bit Duration** as the Broadcast values are (Table 6.2.1.6-1, CR 0009): `sched_info_unpack` of the R2D data carrier has `r_sfs_valid`, and the R2D data vectors at tau/96 use R_SFS 1 (`freq_unicast_cp` 0).
19. **Received Data Size is 0 to 125** (§6.2.1.4); `mac_parse-023` has 125, `-024` has 0.
