# amber/wire.py
"""Encoding of the per-millisecond messages carried in one rfsim block.

A block holds one message: a 4-byte little-endian length, that many bytes of UTF-8 JSON, then
zero padding. An all-zero block means "no message" (for example before a peer has connected).
Command payloads and Tag payloads must therefore be JSON-serializable; the one tuple field Amber
itself sends, data["rx_slots"], is restored as a list of tuples.
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
        raise ValueError(f"message of {size} bytes does not fit in a {block_bytes}-byte block")
    return _LEN.pack(len(body)) + body + bytes(block_bytes - size)


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
    return [[u.bs_id, u.payload, u.rssi_dbm, u.sector_idx, u.subcarrier_shift] for u in uplinks]


def decode_uplinks(encoded: Sequence[list], node_id: int) -> List[Uplink]:
    return [Uplink(bs_id, node_id, payload, rssi, sector, sc) for bs_id, payload, rssi, sector, sc in encoded]
