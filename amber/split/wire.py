# amber/split/wire.py
"""Encoding of the per-millisecond messages carried in one rfsim block.

A block holds one message: a 4-byte little-endian length, that many bytes of UTF-8 JSON, then
zero padding. An all-zero block means "no message" (for example before a peer has connected).
Command payloads and Tag payloads must therefore be plain JSON values: dicts with string keys,
lists, strings, ints, floats, bools and None. encode_deliveries() and encode_uplinks() refuse
anything else, naming the command or transmission and the field, because JSON would either fail or
hand the other side something different (a tuple arrives as a list, a dict key 1 arrives as "1")
and the split run would quietly differ from one process. The one tuple field Amber itself sends,
data["rx_slots"], is allowed and restored as a list of tuples.
"""

import json
import struct
from typing import Any, Dict, List, Optional, Sequence

from .proxies import Delivery, Uplink

_LEN = struct.Struct("<I")


def frame(msg: Dict[str, Any], block_bytes: int) -> bytes:
    """Serialize msg into exactly block_bytes bytes."""
    body = json.dumps(msg, separators=(",", ":")).encode()
    size = _LEN.size + len(body)
    if size > block_bytes:
        raise ValueError(f"{_describe(msg)} is {size} bytes and does not fit in a {block_bytes}-byte block; "
                         "raise block_bytes in the amber section of the OAI configuration, or send fewer "
                         "or smaller commands or payloads in one millisecond")
    return _LEN.pack(len(body)) + body + bytes(block_bytes - size)


def _describe(msg: Dict[str, Any]) -> str:
    """Which message this is, for an error."""
    at = f" at {msg['ms']} ms" if "ms" in msg else ""
    if "cmds" in msg:
        return f"the Reader's message{at}, holding {len(msg['cmds'])} different commands,"
    if "up" in msg:
        return f"a Tag's reply{at}, holding {len(msg['up'])} transmissions,"
    return "a message"


def _plain_json_problem(value: Any, path: str, tuple_ok: bool = False) -> Optional[str]:
    """Why value would not cross as itself in JSON, naming its place under path; None if it would."""
    if value is None or isinstance(value, (str, int, float)):
        return None  # subclasses such as bool or numpy.float64 arrive as equal plain values
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                return f"{path} has the key {key!r} ({type(key).__name__}); JSON turns dict keys into strings"
            problem = _plain_json_problem(item, f"{path}[{key!r}]")
            if problem:
                return problem
        return None
    if isinstance(value, (list, tuple)):
        if isinstance(value, tuple) and not tuple_ok:
            return f"{path} is a tuple, which arrives as a list"
        for i, item in enumerate(value):
            problem = _plain_json_problem(item, f"{path}[{i}]")
            if problem:
                return problem
        return None
    return f"{path} is a {type(value).__name__}, which JSON cannot carry"


def _check_command(d: Delivery):
    if not isinstance(d.data, dict):
        problem = _plain_json_problem(d.data, "data")
    else:
        problem = None
        for key, item in d.data.items():
            if key == "rx_slots" and isinstance(item, list):
                problem = next(filter(None, (_plain_json_problem(slot, f"data['rx_slots'][{i}]", tuple_ok=True)
                                             for i, slot in enumerate(item))), None)
            else:
                problem = _plain_json_problem({key: item}, "data")
            if problem:
                break
    if problem:
        raise ValueError(f"command {d.cmd!r} from base station {d.bs_id} to node {d.node_id}: {problem}. "
                         "Commands must be plain JSON values (string keys, lists, numbers, strings) to cross a split run")


def unframe(block: bytes) -> Optional[Dict[str, Any]]:
    """Parse a block written by frame(); None for an empty block."""
    (length,) = _LEN.unpack_from(block)
    if length == 0:
        return None
    if _LEN.size + length > len(block):
        raise ValueError(f"block announces {length} bytes but holds only {len(block) - _LEN.size}")
    return json.loads(block[_LEN.size:_LEN.size + length])


def encode_deliveries(deliveries: Sequence[Delivery]) -> List[list]:
    """Group consecutive deliveries of the same command (same data object, as BSBehavior sends to
    every addressed node) so a broadcast is encoded once: [[node_ids], cmd, bs_id, data]."""
    out: List[list] = []
    last = None
    for d in deliveries:
        if last is None or last.data is not d.data:
            _check_command(d)
        if last is not None and last.cmd == d.cmd and last.bs_id == d.bs_id and last.data is d.data:
            out[-1][0].append(d.node_id)
        else:
            out.append([[d.node_id], d.cmd, d.bs_id, d.data])
        last = d
    return out


def decode_deliveries(encoded: Sequence[list], node_id: int) -> List[Delivery]:
    """The deliveries addressed to node_id, in the order the Reader made them."""
    out = []
    for node_ids, cmd, bs_id, data in encoded:
        if node_id in node_ids:
            data = dict(data)
            if "rx_slots" in data:
                data["rx_slots"] = [tuple(slot) for slot in data["rx_slots"]]
            out.append(Delivery(node_id, cmd, bs_id, data))
    return out


def encode_uplinks(uplinks: Sequence[Uplink]) -> List[list]:
    for u in uplinks:
        problem = _plain_json_problem(u.payload, "payload")
        if problem:
            raise ValueError(f"node {u.node_id}'s transmission to base station {u.bs_id}: {problem}. "
                             "Tag payloads must be plain JSON values (string keys, lists, numbers, strings) "
                             "to cross a split run")
    return [[u.bs_id, u.payload, u.rssi_dbm, u.sector_idx, u.subcarrier_shift] for u in uplinks]


def decode_uplinks(encoded: Sequence[list], node_id: int) -> List[Uplink]:
    return [Uplink(bs_id, node_id, payload, rssi, sector, sc) for bs_id, payload, rssi, sector, sc in encoded]
