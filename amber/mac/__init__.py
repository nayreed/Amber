"""The A-IoT MAC of TS 38.391 V19.0.0: OAI's C library (openair2/LAYER2/AIOT_MAC, built into
libaiot_phy.so) through ctypes.

The MAC is written once, in C, for every node that runs it: Amber, a gNB as the Reader, a UE as
the Reader. It holds the Reader with its protocols (registration, broadcast, unicast, gen2_q,
adaptive_aloha) and the device. Neither touches the air or a clock: the caller owns the PHY and
the time (samples at 5.76 Msps) and drives them with what happened. aiot_mac.h documents the
API, and how it reads 38.391 where the spec leaves room.

The same library has the messages the nodes send each other over the 5G user plane
(aiot_node_msg.h): Tag data, what a UE that reads the Tags sends to the network (amber.sink).

Reader   next_r2d() -> the caller sends it; r2d_sent() -> the D2R windows to expect;
         d2r_outcome() for each window -> the protocol decides the next R2D.
Device   on_r2d() for each R2D decoded -> the D2R to send, at T_R->D after the R2D's end.

Random numbers come from the caller: a device draws from the rng it is given, so a run repeats
from its seed.
"""

import ctypes as C
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from ..phy import aiot
from ..phy.aiot import MAX_SHIFTS, MAX_SIC_ROUNDS, _Expect, _Outcome, _Params
from ..phy.params import D2RParams

MAX_TBS = 125
MAX_ID_ENTRIES = 8
MAX_PAGING_ID_BITS = 255
MAX_NACK_IDS = 61
MAX_PDU_BITS = 8 * MAX_TBS
MAX_ANSWERS = MAX_ID_ENTRIES * MAX_SHIFTS * MAX_SIC_ROUNDS
MAX_EXPECTS = 16

# aiot_r2d_kind_t, and the names the records give them
PAGING_CBRA, PAGING_CFA, ACCESS_TRIGGER, RANDOM_ID_RESPONSE, R2D_DATA, NACK_FEEDBACK = range(6)
R2D_NAMES = ("paging_cbra", "paging_cfa", "access_trigger", "random_id_response", "r2d_data", "nack_feedback")
# aiot_sched_variant_t: which message carries the Scheduling Info
SCHED_PAGING_CBRA, SCHED_PAGING_CFA, SCHED_RANDOM_ID_RESPONSE, SCHED_R2D_DATA = range(4)
# aiot_answer_kind_t, and the names Answer.kind gives them
ANSWER_MSG1, ANSWER_DATA = range(2)
ANSWER_KINDS = ("msg1", "data")
# aiot_d2r_kind_t
D2R_ACCESS_RANDOM_ID, D2R_UPPER_LAYER_DATA = range(2)
R_SFS_VALUES = (1, 2, 4, 8, 16, 32, 64, 128)
# aiot_d2r_msg_t, as T_R->D names them
D2R_MSG = ("msg1", "msg2", "cfa", "data")
_MOD = {"ook": 0, "bpsk": 1}


# ----------------------------------------------------------------------------------------------
# The C structures (aiot_mac_pdu.h, aiot_mac.h)
# ----------------------------------------------------------------------------------------------

class Sched(C.Structure):
    """aiot_sched_info_t: the D2R Scheduling Info (38.391 6.2.1.6)."""
    _fields_ = [("variant", C.c_int), ("t_bit_num", C.c_int), ("t_bit_den", C.c_int), ("n_sfs", C.c_int),
                ("r_sfs", C.c_int * 8), ("x", C.c_int), ("r_block", C.c_int), ("r_code", C.c_int),
                ("i_bit", C.c_int), ("l_amble", C.c_int), ("i_add", C.c_int), ("d2r_tbs", C.c_int)]

    @property
    def shifts(self) -> List[int]:
        return list(self.r_sfs[:self.n_sfs])


class RidEntry(C.Structure):
    _fields_ = [("echoed_random_id", C.c_int), ("assigned_as_id", C.c_int), ("freq_index", C.c_int)]


class R2DMsg(C.Structure):
    """aiot_mac_r2d_msg_t: one R2D message; the fields used depend on kind."""
    _fields_ = [("kind", C.c_int), ("r2d_tbs", C.c_int), ("sched", Sched), ("transaction_id", C.c_int),
                ("num_access_occasions_code", C.c_int), ("k_bit", C.c_int), ("has_paging_id", C.c_int),
                ("paging_id_len", C.c_int), ("paging_id", C.c_uint8 * MAX_PAGING_ID_BITS),
                ("n_entries", C.c_int), ("entries", RidEntry * MAX_ID_ENTRIES), ("as_id", C.c_int),
                ("has_sdu", C.c_int), ("sdu_len", C.c_int), ("sdu", C.c_uint8 * MAX_TBS),
                ("received_data_size", C.c_int), ("n_as_ids", C.c_int), ("as_ids", C.c_int * MAX_NACK_IDS)]


class D2RMsg(C.Structure):
    """aiot_mac_d2r_msg_t: an Access Random ID (Msg1) or a D2R Upper Layer Data Transfer."""
    _fields_ = [("kind", C.c_int), ("random_id", C.c_int), ("mdi", C.c_int), ("sdu_len", C.c_int),
                ("sdu_", C.c_uint8 * MAX_TBS), ("padding_bytes", C.c_int)]

    @property
    def sdu(self) -> bytes:
        return bytes(self.sdu_[:self.sdu_len])


class LinkConfig_(C.Structure):
    _fields_ = [("m_chip_symb", C.c_int), ("n_rb", C.c_int), ("t_bit_num", C.c_int), ("t_bit_den", C.c_int),
                ("n_r_sfs", C.c_int), ("r_sfs", C.c_int * 8), ("x", C.c_int), ("r_code", C.c_int),
                ("r_block", C.c_int), ("l_amble", C.c_int), ("interval_cp", C.c_int), ("i_add", C.c_int),
                ("d2r_tbs", C.c_int)]


class Param_(C.Structure):
    _fields_ = [("name", C.c_char_p), ("value", C.c_double)]


class ReaderR2D_(C.Structure):
    _fields_ = [("msg", R2DMsg), ("bits", C.c_uint8 * MAX_PDU_BITS), ("n_bits", C.c_int),
                ("earliest_start", C.c_int64)]


class Answer_(C.Structure):
    _fields_ = [("kind", C.c_int), ("message", D2RMsg), ("r_sfs", C.c_int), ("rssi_w", C.c_double),
                ("start", C.c_int64), ("slot", C.c_int), ("entry", C.c_int), ("as_id", C.c_int)]


class Heard_(C.Structure):
    _fields_ = [("sent", R2DMsg), ("r2d_start", C.c_int64), ("r2d_end", C.c_int64), ("n_answers", C.c_int),
                ("answers", Answer_ * MAX_ANSWERS), ("collided", C.c_int)]


class TagData_(C.Structure):
    _fields_ = [("seq", C.c_uint32), ("after", C.c_int), ("answer", Answer_)]


_MATCHED = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(C.c_uint8), C.c_int)
_SELECTED = C.CFUNCTYPE(None, C.c_void_p)
_DELIVER = C.CFUNCTYPE(None, C.c_void_p, C.POINTER(C.c_uint8), C.c_int)
_NEXT_SDU = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(C.c_uint8), C.c_int)
_DRAW = C.CFUNCTYPE(C.c_uint32, C.c_void_p, C.c_uint32)


class DeviceUpper_(C.Structure):
    _fields_ = [("user", C.c_void_p), ("paging_id_matched", _MATCHED), ("selected", _SELECTED),
                ("deliver", _DELIVER), ("next_sdu", _NEXT_SDU), ("draw", _DRAW)]


class DeviceSend_(C.Structure):
    _fields_ = [("message", D2RMsg), ("bits", C.c_uint8 * MAX_PDU_BITS), ("n_bits", C.c_int),
                ("params", _Params), ("msg", C.c_int), ("tbs_bytes", C.c_int), ("slot", C.c_int),
                ("n_candidates", C.c_int), ("candidates", C.c_int * 8)]


class DeviceState(C.Structure):
    """aiot_device_state_t: the device's stored state; -1 means none."""
    _fields_ = [("transaction_id", C.c_int), ("as_id", C.c_int), ("failed_tid", C.c_int),
                ("cbra_active", C.c_int), ("cbra_success", C.c_int), ("counter", C.c_int),
                ("random_id", C.c_int), ("msg2_budget", C.c_int), ("cfa_in_procedure", C.c_int),
                ("nack_monitoring", C.c_int), ("sending_len", C.c_int)]


_STRUCTS = {"aiot_d2r_params_t": _Params, "aiot_sched_info_t": Sched, "aiot_mac_r2d_msg_t": R2DMsg,
            "aiot_mac_d2r_msg_t": D2RMsg, "aiot_link_config_t": LinkConfig_, "aiot_param_t": Param_,
            "aiot_reader_r2d_t": ReaderR2D_, "aiot_reader_answer_t": Answer_, "aiot_reader_heard_t": Heard_,
            "aiot_d2r_expect_t": _Expect, "aiot_d2r_outcome_t": _Outcome, "aiot_device_upper_t": DeviceUpper_,
            "aiot_device_send_t": DeviceSend_, "aiot_device_state_t": DeviceState, "aiot_tag_data_t": TagData_}

_lib = None


def _L():
    """libaiot_phy with the MAC's functions declared, its structures checked against these."""
    global _lib
    if _lib is not None:
        return _lib
    lib = aiot.load()
    if not hasattr(lib, "aiot_mac_struct_size"):
        raise RuntimeError(f"{aiot.lib_path()} has no A-IoT MAC: build it from an OAI checkout that has "
                           "openair2/LAYER2/AIOT_MAC (scripts/build_aiot_lib.sh)")
    lib.aiot_mac_struct_size.argtypes = [C.c_char_p]
    for name, cls in _STRUCTS.items():
        size = lib.aiot_mac_struct_size(name.encode())
        if size != C.sizeof(cls):
            raise RuntimeError(f"{aiot.lib_path()}: {name} is {size} bytes, Amber's copy {C.sizeof(cls)}: "
                               "the library and Amber are from different versions")
    vp, ip, i64 = C.c_void_p, C.c_int, C.c_int64
    u8p = C.POINTER(C.c_uint8)
    lib.aiot_link_config_check.argtypes = [C.POINTER(LinkConfig_)]
    lib.aiot_protocol_count.argtypes = []
    lib.aiot_protocol_name.argtypes = [ip]
    lib.aiot_protocol_name.restype = C.c_char_p
    lib.aiot_protocol_params.argtypes = [C.c_char_p, C.POINTER(C.c_char_p), C.POINTER(C.c_double),
                                         C.POINTER(C.c_int), ip]
    lib.aiot_reader_new.argtypes = [C.POINTER(LinkConfig_), C.c_char_p, C.POINTER(Param_), ip, C.POINTER(C.c_int),
                                    ip, C.c_char_p, ip]
    lib.aiot_reader_new.restype = vp
    lib.aiot_reader_free.argtypes = [vp]
    lib.aiot_reader_free.restype = None
    lib.aiot_reader_next_r2d.argtypes = [vp, i64, C.POINTER(ReaderR2D_)]
    lib.aiot_reader_r2d_sent.argtypes = [vp, i64, i64, i64, C.POINTER(_Expect), ip]
    lib.aiot_reader_d2r_outcome.argtypes = [vp, C.POINTER(_Outcome), i64]
    lib.aiot_reader_done.argtypes = [vp]
    lib.aiot_reader_pop_heard.argtypes = [vp, C.POINTER(Heard_)]
    lib.aiot_reader_table.argtypes = [vp, C.POINTER(C.c_char_p), C.POINTER(C.POINTER(C.c_char_p)),
                                      C.POINTER(C.c_int)]
    lib.aiot_reader_pop_rows.argtypes = [vp, C.POINTER(C.c_double), ip]
    lib.aiot_device_new.argtypes = [C.POINTER(DeviceUpper_), ip]
    lib.aiot_device_new.restype = vp
    lib.aiot_device_free.argtypes = [vp]
    lib.aiot_device_free.restype = None
    lib.aiot_device_on_r2d.argtypes = [vp, C.POINTER(R2DMsg), C.POINTER(DeviceSend_)]
    lib.aiot_device_send_offset.argtypes = [C.POINTER(DeviceSend_), ip]
    lib.aiot_device_state.argtypes = [vp, C.POINTER(DeviceState)]
    lib.aiot_device_state.restype = None
    lib.aiot_mac_parse_r2d.argtypes = [u8p, ip, C.POINTER(R2DMsg)]
    lib.aiot_mac_pack_r2d.argtypes = [C.POINTER(R2DMsg), u8p, ip]
    lib.aiot_mac_num_access_occasions.argtypes = [ip]
    lib.aiot_mac_k_value.argtypes = [ip]
    lib.aiot_mac_freq_index_to_r_sfs.argtypes = [ip]
    lib.aiot_mac_r2d_type_valid.argtypes = [ip]
    lib.aiot_mac_r2d_natural_bytes.argtypes = [C.POINTER(R2DMsg)]
    lib.aiot_mac_r2d_body_bits.argtypes = [C.POINTER(R2DMsg)]
    lib.aiot_mac_pack_d2r.argtypes = [C.POINTER(D2RMsg), u8p, ip]
    lib.aiot_mac_parse_d2r.argtypes = [u8p, ip, ip, ip, C.POINTER(D2RMsg)]
    lib.aiot_mac_d2r_for_tbs.argtypes = [u8p, ip, ip, ip, C.POINTER(D2RMsg)]
    lib.aiot_mac_sched_width.argtypes = [ip]
    lib.aiot_mac_sched_validate.argtypes = [C.POINTER(Sched)]
    lib.aiot_mac_sched_pack.argtypes = [C.POINTER(Sched), u8p, ip]
    lib.aiot_mac_sched_parse.argtypes = [u8p, ip, ip, C.POINTER(Sched)]
    lib.aiot_mac_to_d2r_params.argtypes = [C.POINTER(Sched), ip, ip, C.POINTER(_Params)]
    lib.aiot_reader_push.argtypes = [vp, C.POINTER(R2DMsg), ip]
    lib.aiot_reader_idle.argtypes = [vp]
    lib.aiot_node_msg_type.argtypes = [u8p, ip]
    lib.aiot_node_msg_pack_tag_data.argtypes = [C.POINTER(TagData_), u8p, ip]
    lib.aiot_node_msg_parse_tag_data.argtypes = [u8p, ip, C.POINTER(TagData_)]
    _lib = lib
    return lib


def _link(cfg) -> LinkConfig_:
    """The C link settings of an amber.link.LinkConfig."""
    bit = Fraction(cfg.t_bit_tau)
    if len(cfg.r_sfs) > 8:
        raise ValueError(f"at most 8 R_SFS values, got {list(cfg.r_sfs)}")
    return LinkConfig_(cfg.m_chip_symb, cfg.n_rb, bit.numerator, bit.denominator, len(cfg.r_sfs),
                       (C.c_int * 8)(*cfg.r_sfs), cfg.x, int(cfg.r_code), cfg.r_block, cfg.l_amble,
                       cfg.interval_cp, int(cfg.i_add), cfg.d2r_tbs)


def link_config_check(cfg) -> int:
    """0 when the link settings make a valid CBRA Scheduling Info (38.391 Table 6.2.1.6-1)."""
    c = _link(cfg)
    return _L().aiot_link_config_check(C.byref(c))


def d2r_params(p: _Params) -> D2RParams:
    """The Python D2RParams of a C aiot_d2r_params_t."""
    return D2RParams(t_bit_tau=Fraction(p.t_bit_num, p.t_bit_den), r_sfs=p.r_sfs, r_block=p.r_block,
                     r_code=bool(p.r_code), l_amble=p.l_amble, i_bit=p.i_bit, i_add=bool(p.i_add),
                     modulation="bpsk" if p.modulation == _MOD["bpsk"] else "ook")


def _u8(bits: Sequence[int]):
    return (C.c_uint8 * max(1, len(bits)))(*bits)


# ----------------------------------------------------------------------------------------------
# Messages (38.391 clause 6): build, pack, parse
# ----------------------------------------------------------------------------------------------

def sched(variant: int, r_sfs: Sequence[int], d2r_tbs: int = 0, x: int = 0, t_bit_tau=Fraction(1, 8),
          r_block: int = 1, r_code: bool = False, i_bit: int = 0, l_amble: int = 31, i_add: bool = False) -> Sched:
    """A D2R Scheduling Info; ValueError if 38.391 Table 6.2.1.6-1 does not allow it. i_bit 0 is
    codepoint 0; d2r_tbs 0 is absent (CBRA paging); x is for a CBRA paging only."""
    t = Fraction(t_bit_tau)
    r = sorted(r_sfs)
    s = Sched(variant, t.numerator, t.denominator, len(r), (C.c_int * 8)(*r), x, r_block, int(r_code), i_bit,
              l_amble, int(i_add), d2r_tbs)
    if _L().aiot_mac_sched_validate(C.byref(s)) < 0:
        raise ValueError(f"not a valid D2R Scheduling Info: variant {variant}, T_bit {t}, R_SFS {r}, x {x}, "
                         f"R_block {r_block}, I_bit {i_bit}, L_amble {l_amble}, D2R TBS {d2r_tbs}")
    return s


def sched_width(variant: int) -> int:
    return _check(_L().aiot_mac_sched_width(variant), "Scheduling Info width")


def sched_pack(s: Sched) -> List[int]:
    b = (C.c_uint8 * 64)()
    n = _check(_L().aiot_mac_sched_pack(C.byref(s), b, 64), "Scheduling Info pack")
    return list(b[:n])


def sched_parse(bits: Sequence[int], variant: int) -> Sched:
    s = Sched()
    _check(_L().aiot_mac_sched_parse(_u8(bits), len(bits), variant, C.byref(s)), "Scheduling Info parse")
    return s


def to_d2r_params(s: Sched, r_sfs: int, modulation: str = "bpsk") -> D2RParams:
    """The L1 parameters of one D2R: the Scheduling Info with the device's own R_SFS."""
    p = _Params()
    _check(_L().aiot_mac_to_d2r_params(C.byref(s), r_sfs, _MOD[modulation], C.byref(p)), "to_d2r_params")
    return d2r_params(p)


def _check(rc: int, what: str) -> int:
    if rc < 0:
        raise ValueError(f"{what}: not valid in 38.391 (error {rc})")
    return rc


def _bitlist(dst, bits: Optional[Sequence[int]]) -> Tuple[int, int]:
    if bits is None:
        return 0, 0
    if len(bits) > MAX_PAGING_ID_BITS:
        raise ValueError(f"a Paging ID has at most {MAX_PAGING_ID_BITS} bits, got {len(bits)}")
    for i, b in enumerate(bits):
        dst[i] = b
    return 1, len(bits)


def paging_cbra(tid: int, s: Sched, n_occasions_code: int = 0, k_bit: int = 0,
                paging_id: Optional[Sequence[int]] = None, r2d_tbs: int = 0) -> R2DMsg:
    m = R2DMsg(kind=PAGING_CBRA, r2d_tbs=r2d_tbs, sched=s, transaction_id=tid,
               num_access_occasions_code=n_occasions_code, k_bit=k_bit)
    m.has_paging_id, m.paging_id_len = _bitlist(m.paging_id, paging_id)
    return m


def paging_cfa(paging_id: Sequence[int], s: Sched, r2d_tbs: int = 0) -> R2DMsg:
    m = R2DMsg(kind=PAGING_CFA, r2d_tbs=r2d_tbs, sched=s)
    m.has_paging_id, m.paging_id_len = _bitlist(m.paging_id, paging_id)
    return m


def access_trigger() -> R2DMsg:
    return R2DMsg(kind=ACCESS_TRIGGER)


def random_id_response(entries: Sequence[Tuple[int, Optional[int], Optional[int]]], s: Sched,
                       r2d_tbs: int = 0) -> R2DMsg:
    """entries: (Echoed Random ID, Assigned AS ID or None, Frequency Index codepoint or None)."""
    if not 1 <= len(entries) <= MAX_ID_ENTRIES:
        raise ValueError(f"a Random ID Response has 1 to {MAX_ID_ENTRIES} entries, got {len(entries)}")
    m = R2DMsg(kind=RANDOM_ID_RESPONSE, r2d_tbs=r2d_tbs, sched=s, n_entries=len(entries))
    for i, (rid, as_id, fi) in enumerate(entries):
        m.entries[i] = RidEntry(rid, -1 if as_id is None else as_id, -1 if fi is None else fi)
    return m


def r2d_data(as_id: int, s: Sched, sdu: Optional[bytes] = None, received: Optional[int] = None,
             r2d_tbs: int = 0) -> R2DMsg:
    """R2D Upper Layer Data Transfer: a Data SDU, or the Received Data Size."""
    m = R2DMsg(kind=R2D_DATA, r2d_tbs=r2d_tbs, sched=s, as_id=as_id)
    if sdu is not None:
        if len(sdu) > MAX_TBS:
            raise ValueError(f"a Data SDU has at most {MAX_TBS} bytes, got {len(sdu)}")
        m.has_sdu, m.sdu_len = 1, len(sdu)
        m.sdu[:len(sdu)] = list(sdu)
    else:
        m.received_data_size = received or 0
    return m


def nack(as_ids: Sequence[int], r2d_tbs: int = 0) -> R2DMsg:
    if not 1 <= len(as_ids) <= MAX_NACK_IDS:
        raise ValueError(f"NACK Feedback carries 1 to {MAX_NACK_IDS} AS IDs, got {len(as_ids)}")
    m = R2DMsg(kind=NACK_FEEDBACK, r2d_tbs=r2d_tbs, n_as_ids=len(as_ids))
    m.as_ids[:len(as_ids)] = list(as_ids)
    return m


def r2d_body_bits(m: R2DMsg) -> int:
    """Bits of the message before any Fill Bits."""
    return _check(_L().aiot_mac_r2d_body_bits(C.byref(m)), "R2D message")


def access_random_id(random_id: int) -> D2RMsg:
    return D2RMsg(kind=D2R_ACCESS_RANDOM_ID, random_id=random_id)


def d2r_data(sdu: bytes, mdi: int = 0, padding_bytes: int = 0) -> D2RMsg:
    if len(sdu) > MAX_TBS:
        raise ValueError(f"a Data SDU has at most {MAX_TBS} bytes, got {len(sdu)}")
    m = D2RMsg(kind=D2R_UPPER_LAYER_DATA, mdi=mdi, sdu_len=len(sdu), padding_bytes=padding_bytes)
    m.sdu_[:len(sdu)] = list(sdu)
    return m


def d2r_for_tbs(sdu: bytes, tbs: int, mdi: int = 0) -> D2RMsg:
    """Upper Layer Data padded to exactly tbs bytes."""
    m = D2RMsg()
    _check(_L().aiot_mac_d2r_for_tbs(_u8(list(sdu)), len(sdu), tbs, mdi, C.byref(m)), "D2R data for TBS")
    return m


def pack_d2r(m: D2RMsg) -> List[int]:
    b = (C.c_uint8 * MAX_PDU_BITS)()
    n = _check(_L().aiot_mac_pack_d2r(C.byref(m), b, MAX_PDU_BITS), "D2R pack")
    return list(b[:n])


def parse_d2r(bits: Sequence[int], kind: int, d2r_tbs: int = 0) -> D2RMsg:
    m = D2RMsg()
    _check(_L().aiot_mac_parse_d2r(_u8(bits), len(bits), kind, d2r_tbs, C.byref(m)), "D2R parse")
    return m


def num_access_occasions(code: int) -> int:
    return _check(_L().aiot_mac_num_access_occasions(code), "Number of Access Occasions")


def k_value(k_bit: int) -> int:
    return _check(_L().aiot_mac_k_value(k_bit), "K")


def freq_index_r_sfs(freq_index: int) -> int:
    return _check(_L().aiot_mac_freq_index_to_r_sfs(freq_index), "Frequency Index")


def r2d_type_valid(type3: int) -> bool:
    return bool(_L().aiot_mac_r2d_type_valid(type3))


def parse_r2d(bits: Sequence[int]) -> Optional[R2DMsg]:
    """The R2D message in bits, or None when they are not one."""
    m = R2DMsg()
    return m if _L().aiot_mac_parse_r2d(_u8(bits), len(bits), C.byref(m)) == 0 else None


def pack_r2d(m: R2DMsg) -> List[int]:
    b = (C.c_uint8 * MAX_PDU_BITS)()
    n = _check(_L().aiot_mac_pack_r2d(C.byref(m), b, MAX_PDU_BITS), "R2D pack")
    return list(b[:n])


def r2d_name(kind: int) -> str:
    """The name the records give an R2D message of this aiot_r2d_kind_t."""
    return R2D_NAMES[kind] if 0 <= kind < len(R2D_NAMES) else f"kind {kind}"


def describe(m: R2DMsg) -> Tuple[str, str]:
    """(message name, its main fields) of an R2D message, for reader_tx.csv."""
    name = r2d_name(m.kind)
    if m.kind == PAGING_CBRA:
        n = _L().aiot_mac_num_access_occasions(m.num_access_occasions_code)
        return name, f"tid={m.transaction_id} n={n} k={4 if m.k_bit else 1} x={m.sched.x} r_sfs={m.sched.shifts}"
    if m.kind == PAGING_CFA:
        return name, f"paging_id={m.paging_id_len} bits r_sfs={m.sched.r_sfs[0]}"
    if m.kind == RANDOM_ID_RESPONSE:
        return name, f"entries={m.n_entries}"
    if m.kind == R2D_DATA:
        return name, f"as_id={m.as_id} ci={1 if m.has_sdu else 0}"
    if m.kind == NACK_FEEDBACK:
        return name, f"as_ids={list(m.as_ids[:m.n_as_ids])}"
    return name, ""


# ----------------------------------------------------------------------------------------------
# Protocols
# ----------------------------------------------------------------------------------------------

def protocol_names() -> List[str]:
    lib = _L()
    return [lib.aiot_protocol_name(i).decode() for i in range(lib.aiot_protocol_count())]


def protocol_params(name: str) -> Dict[str, Tuple[float, bool]]:
    """{parameter: (default, takes whole numbers only)} of a protocol."""
    names, defaults, is_int = (C.c_char_p * 8)(), (C.c_double * 8)(), (C.c_int * 8)()
    n = _L().aiot_protocol_params(name.encode(), names, defaults, is_int, 8)
    if n < 0:
        raise ValueError(f"unknown protocol {name!r}")
    return {names[i].decode(): (defaults[i], bool(is_int[i])) for i in range(n)}


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "an integer"
    if isinstance(value, float):
        return "a number"
    return f"a {type(value).__name__}"


# ----------------------------------------------------------------------------------------------
# Reader
# ----------------------------------------------------------------------------------------------

@dataclass
class Answer:
    """One D2R the Reader decoded.

    kind is "msg1" (message.random_id) or "data" (message.sdu, message.mdi). slot is the Msg1
    time slot, entry the Random ID Response entry it answers (or None), as_id the AS ID the data
    came from, if known."""
    kind: str
    message: D2RMsg
    r_sfs: int
    rssi_w: float
    start: int
    slot: int = 0
    entry: Optional[int] = None
    as_id: Optional[int] = None


@dataclass
class Heard:
    """What followed one R2D message: the answers decoded and the preambles found whose frame did
    not decode (collisions, or a frame lost in noise after its preamble was seen)."""
    sent: R2DMsg
    answers: List[Answer]
    collided: int
    r2d_start: int
    r2d_end: int

    @property
    def empty(self) -> bool:
        return not self.answers and not self.collided


def _answer(a: Answer_) -> Answer:
    m = D2RMsg()
    C.pointer(m)[0] = a.message
    return Answer(ANSWER_KINDS[a.kind], m, a.r_sfs, a.rssi_w, a.start, a.slot,
                  None if a.entry < 0 else a.entry, None if a.as_id < 0 else a.as_id)


def _answer_c(a: Answer) -> Answer_:
    if a.kind not in ANSWER_KINDS:
        raise ValueError(f"an answer of kind {a.kind!r}, not one of {ANSWER_KINDS}")
    return Answer_(kind=ANSWER_KINDS.index(a.kind), message=a.message, r_sfs=a.r_sfs, rssi_w=a.rssi_w,
                   start=a.start, slot=a.slot, entry=-1 if a.entry is None else a.entry,
                   as_id=-1 if a.as_id is None else a.as_id)


def _heard(h: Heard_) -> Heard:
    out = [_answer(a) for a in h.answers[:h.n_answers]]
    sent = R2DMsg()
    C.pointer(sent)[0] = h.sent
    return Heard(sent, out, h.collided, h.r2d_start, h.r2d_end)


class Reader:
    """A Reader running a protocol of the library on a link, for the Tags node_ids.

    cfg is an amber.link.LinkConfig (any object with its fields); params the protocol's
    parameters by name. A bad name, parameter or setting raises ValueError."""

    def __init__(self, cfg, protocol: str, params: Mapping[str, Any], node_ids: Sequence[int]):
        lib = _L()
        self.name = protocol
        self.cfg = cfg
        c = _link(cfg)
        ps = (Param_ * max(1, len(params)))(*[Param_(k.encode(), float(v)) for k, v in params.items()])
        ids = (C.c_int * max(1, len(node_ids)))(*node_ids)
        err = C.create_string_buffer(256)
        self._r = lib.aiot_reader_new(C.byref(c), protocol.encode(), ps, len(params), ids, len(node_ids), err, 256)
        if not self._r:
            msg = err.value.decode()
            raise ValueError(msg if msg.startswith(("protocol ", "no protocol")) else
                             f"protocol {protocol}: {msg}")
        self._r2d = ReaderR2D_()
        self._exp = (_Expect * MAX_EXPECTS)()
        self._h = Heard_()
        self._rows: List[Tuple] = []

    def __del__(self):
        if getattr(self, "_r", None) and _lib is not None:
            _lib.aiot_reader_free(self._r)
            self._r = None

    @property
    def done(self) -> bool:
        return bool(_L().aiot_reader_done(self._r))

    def next_r2d(self, now: int) -> Optional[Tuple[R2DMsg, List[int], int]]:
        """(message, its bits, the earliest sample it may start at), or None while answers are
        awaited or once the protocol has ended."""
        rc = _L().aiot_reader_next_r2d(self._r, now, C.byref(self._r2d))
        if rc < 0:
            raise ValueError(f"the protocol's message does not pack (error {rc})")
        if rc == 0:
            return None
        return self._r2d.msg, list(self._r2d.bits[:self._r2d.n_bits]), int(self._r2d.earliest_start)

    def r2d_sent(self, start: int, end: int, now: int) -> List[_Expect]:
        """The R2D next_r2d() gave went out over [start, end): the D2R windows to expect."""
        n = _L().aiot_reader_r2d_sent(self._r, start, end, now, self._exp, MAX_EXPECTS)
        if n < 0:
            raise ValueError(f"aiot_reader_r2d_sent failed (error {n})")
        out = []
        for e in self._exp[:n]:
            copy = _Expect()
            C.pointer(copy)[0] = e
            out.append(copy)
        return out

    def d2r_outcome(self, expect: _Expect, decoded: Sequence[aiot.D2RResult], collided: int, now: int):
        o = _Outcome()
        o.expect = expect
        o.collided = collided
        for i, r in enumerate(decoded[:MAX_SHIFTS * MAX_SIC_ROUNDS]):
            d = o.decoded[i]
            d.bits[:len(r.bits)] = r.bits
            d.n_bits, d.r_sfs, d.start, d.rssi_w, d.tag = len(r.bits), r.r_sfs, r.start, r.rssi_w, expect.tag
        o.n_decoded = min(len(decoded), MAX_SHIFTS * MAX_SIC_ROUNDS)
        if _L().aiot_reader_d2r_outcome(self._r, C.byref(o), now) < 0:
            raise ValueError("an outcome the Reader did not expect")

    @property
    def idle(self) -> bool:
        """Nothing to send and no answers to wait for: push() may give the next message."""
        return bool(_L().aiot_reader_idle(self._r))

    def push(self, m: R2DMsg, access: bool = False):
        """Send m next (the protocol "manual" sends only these). With access, a CBRA paging's or
        Access Trigger's Msg1s get the Reader's Random ID Response."""
        if _L().aiot_reader_push(self._r, C.byref(m), int(access)) < 0:
            raise ValueError("the Reader is not idle, or the message is not valid")

    def pop_heard(self) -> List[Heard]:
        """What followed each R2D whose answers are all in, oldest first."""
        out = []
        while _L().aiot_reader_pop_heard(self._r, C.byref(self._h)) == 1:
            out.append(_heard(self._h))
        return out

    def table(self) -> Optional[Tuple[str, List[str], List[Tuple[float, ...]]]]:
        """The protocol's own table (q.csv, adaptation.csv): (file name, columns, rows), or None.
        The C table counts time in ms; here it is in s, rounded to the microsecond."""
        name, cols, nc = C.c_char_p(), C.POINTER(C.c_char_p)(), C.c_int()
        lib = _L()
        if not lib.aiot_reader_table(self._r, C.byref(name), C.byref(cols), C.byref(nc)):
            return None
        n = nc.value
        buf = (C.c_double * (64 * n))()
        while True:
            k = lib.aiot_reader_pop_rows(self._r, buf, 64)
            if k <= 0:
                break
            for i in range(k):
                row = buf[i * n:(i + 1) * n]
                self._rows.append((round(row[0] / 1000.0, 6),) + tuple(int(v) for v in row[1:]))
        columns = [cols[i].decode() for i in range(n)]
        if columns and columns[0] == "time_ms":
            columns[0] = "time_s"
        return name.value.decode(), columns, list(self._rows)


# ----------------------------------------------------------------------------------------------
# Device
# ----------------------------------------------------------------------------------------------

class UpperLayer:
    """What the device's MAC asks of the layer above it. The defaults: every Paging ID matches,
    nothing is delivered or sent."""

    def paging_id_matched(self, paging_id: Sequence[int]) -> bool:
        return True

    def selected(self) -> None:
        pass

    def deliver(self, sdu: bytes) -> None:
        pass

    def next_sdu(self) -> Optional[bytes]:
        return None


@dataclass
class D2RSend:
    """One D2R the device's MAC asks the PHY to send. msg is the kind T_R->D needs ("msg1",
    "msg2", "cfa" or "data"); slot the Msg1 access occasion's time slot; candidate_r_sfs the set
    T'_chip is taken over (Msg1 and Msg2)."""
    pdu: List[int]
    params: D2RParams
    msg: str
    tbs_bytes: int
    message: D2RMsg
    slot: int
    candidate_r_sfs: Tuple[int, ...]
    _c: DeviceSend_

    def offset(self, m_chip_symb: int) -> int:
        """T_R->D: samples from the end of the R2D it answers to its first sample."""
        off = _L().aiot_device_send_offset(C.byref(self._c), m_chip_symb)
        if off < 0:
            raise ValueError(f"no T_R->D for this D2R (error {off})")
        return off


class Device:
    """The device's MAC (38.391 5.2 to 5.5). upper is its UpperLayer; rng draws the access
    occasion and the Random ID, as rng.integers(0, n); modulation is its own choice, not
    signalled (38.291 7.1.3)."""

    def __init__(self, upper: Optional[UpperLayer] = None, rng: Optional[np.random.Generator] = None,
                 modulation: str = "bpsk"):
        if modulation not in _MOD:
            raise ValueError(f"modulation must be 'bpsk' or 'ook', got {modulation!r}")
        self.upper = upper or UpperLayer()
        self.rng = rng or np.random.default_rng()
        self.modulation = modulation
        self._error: Optional[BaseException] = None
        # the callbacks must live as long as the device
        self._cb = DeviceUpper_(None, _MATCHED(self._matched), _SELECTED(self._selected), _DELIVER(self._deliver),
                                _NEXT_SDU(self._next_sdu), _DRAW(self._draw))
        self._d = _L().aiot_device_new(C.byref(self._cb), _MOD[modulation])
        if not self._d:
            raise MemoryError("aiot_device_new")
        self._send = DeviceSend_()

    def __del__(self):
        if getattr(self, "_d", None) and _lib is not None:
            _lib.aiot_device_free(self._d)
            self._d = None

    # an exception in a callback cannot cross C: keep it and raise it after
    def _guard(self, f, default):
        try:
            return f()
        except BaseException as e:   # noqa: BLE001
            self._error = self._error or e
            return default

    def _matched(self, _user, bits, n):
        return self._guard(lambda: int(bool(self.upper.paging_id_matched([bits[i] for i in range(n)]))), 0)

    def _selected(self, _user):
        self._guard(self.upper.selected, None)

    def _deliver(self, _user, sdu, n):
        self._guard(lambda: self.upper.deliver(bytes(sdu[:n])), None)

    def _next_sdu(self, _user, buf, cap):
        def take():
            sdu = self.upper.next_sdu()
            if sdu is None:
                return -1
            if len(sdu) > cap:
                raise ValueError(f"an SDU of {len(sdu)} bytes is more than the device holds ({cap})")
            C.memmove(buf, bytes(sdu), len(sdu))
            return len(sdu)
        return self._guard(take, -1)

    def _draw(self, _user, n):
        return self._guard(lambda: int(self.rng.integers(0, n)), 0)

    def on_r2d(self, m: R2DMsg) -> List[D2RSend]:
        """Handle one R2D message the device decoded: the D2R it sends, if any."""
        rc = _L().aiot_device_on_r2d(self._d, C.byref(m), C.byref(self._send))
        if self._error is not None:
            e, self._error = self._error, None
            raise e
        if rc < 0:
            raise ValueError(f"the device cannot act on this R2D message (error {rc})")
        if rc == 0:
            return []
        s = DeviceSend_()
        C.pointer(s)[0] = self._send
        msg = D2RMsg()
        C.pointer(msg)[0] = s.message
        return [D2RSend(list(s.bits[:s.n_bits]), d2r_params(s.params), D2R_MSG[s.msg], s.tbs_bytes, msg, s.slot,
                        tuple(s.candidates[:s.n_candidates]), s)]

    def state(self) -> DeviceState:
        st = DeviceState()
        _L().aiot_device_state(self._d, C.byref(st))
        return st


# ----------------------------------------------------------------------------------------------
# Messages between nodes (aiot_node_msg.h)
# ----------------------------------------------------------------------------------------------

# Copies of aiot_node_msg.h's AIOT_NODE_MSG_TAG_DATA and AIOT_TAG_DATA_FIXED_BYTES (a #define is not in
# the library); tests/test_sink.py checks them against the messages the library packs.
NODE_MSG_TAG_DATA = 1
TAG_DATA_FIXED_BYTES = 40
NODE_MSG_MAX_BYTES = TAG_DATA_FIXED_BYTES + MAX_TBS


@dataclass
class TagData:
    """One message a Reader heard from a Tag, as the Reader sends it on: seq counts the sender's
    Tag data messages (a lost one leaves a gap), after is the aiot_r2d_kind_t of the R2D the Tag
    answered."""
    seq: int
    after: int
    answer: Answer


def node_msg_type(data: bytes) -> Optional[int]:
    """The type of the message in data, or None when it is not one whole message."""
    t = _L().aiot_node_msg_type(_u8(data), len(data))
    return None if t < 0 else t


def pack_tag_data(m: TagData) -> bytes:
    """The datagram of m; ValueError for a field the layout cannot hold."""
    if not 0 <= m.seq < 1 << 32:
        raise ValueError(f"Tag data seq {m.seq}: not a 32-bit count")
    c = TagData_(seq=m.seq, after=m.after, answer=_answer_c(m.answer))
    b = (C.c_uint8 * NODE_MSG_MAX_BYTES)()
    n = _check(_L().aiot_node_msg_pack_tag_data(C.byref(c), b, NODE_MSG_MAX_BYTES), f"Tag data seq {m.seq} pack")
    return bytes(b[:n])


def parse_tag_data(data: bytes) -> TagData:
    """The Tag data message in data; ValueError when it is not one."""
    c = TagData_()
    if _L().aiot_node_msg_parse_tag_data(_u8(data), len(data), C.byref(c)) != 0:
        raise ValueError(f"{len(data)} bytes that are not one Tag data message")
    return TagData(c.seq, c.after, _answer(c.answer))
