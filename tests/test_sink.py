# tests/test_sink.py
"""Tag data (OAI's aiot_node_msg.c in libaiot_phy.so, through amber.mac) and the sink that writes it
down: the bytes where the layout puts them, a round trip, and tag_data.csv in the columns and the
number formats of the OAI Reader's reader_rx.csv."""

import csv
import os
import socket
import struct

import pytest

from amber import mac, sink
from amber.iq import make_sdu


def msg1(seq=0, random_id=0xBEEF, start=5760 * 1062, after=mac.PAGING_CBRA, rssi_w=1.0e3):
    a = mac.Answer("msg1", mac.access_random_id(random_id), r_sfs=2, rssi_w=rssi_w, start=start, slot=1)
    return mac.TagData(seq, after, a)


def data(seq=1, node=3, value=42, as_id=7, start=5760 * 1080 + 96, after=mac.RANDOM_ID_RESPONSE, rssi_w=2.5e2):
    a = mac.Answer("data", mac.d2r_data(make_sdu(node, value), mdi=1, padding_bytes=1), r_sfs=4, rssi_w=rssi_w,
                   start=start, slot=0, entry=0, as_id=as_id)
    return mac.TagData(seq, after, a)


# ----------------------------------------------------------------------------------------------
# The message
# ----------------------------------------------------------------------------------------------

def test_tag_data_bytes_are_where_the_layout_says():
    b = mac.pack_tag_data(data())
    assert len(b) == mac.TAG_DATA_FIXED_BYTES + 3
    version, kind, length, seq, start = struct.unpack(">BBHIq", b[:16])
    assert (version, kind, length, seq, start) == (1, mac.NODE_MSG_TAG_DATA, len(b), 1, 5760 * 1080 + 96)
    assert struct.unpack(">d", b[16:24])[0] == 2.5e2
    assert struct.unpack(">BBBBhhiHBB", b[24:40]) == (mac.RANDOM_ID_RESPONSE, 1, 4, 1, 0, 0, 7, 0, 1, 3)
    assert b[40:] == make_sdu(3, 42)
    assert mac.node_msg_type(b) == mac.NODE_MSG_TAG_DATA


@pytest.mark.parametrize("m", [msg1(), data(), data(as_id=None, seq=2**32 - 1)])
def test_tag_data_comes_back_as_it_went(m):
    back = mac.parse_tag_data(mac.pack_tag_data(m))
    assert (back.seq, back.after) == (m.seq, m.after)
    a, b = m.answer, back.answer
    assert (b.kind, b.r_sfs, b.rssi_w, b.start, b.slot, b.entry, b.as_id) == \
           (a.kind, a.r_sfs, a.rssi_w, a.start, a.slot, a.entry, a.as_id)
    assert (b.message.kind, b.message.random_id, b.message.mdi, b.message.sdu, b.message.padding_bytes) == \
           (a.message.kind, a.message.random_id, a.message.mdi, a.message.sdu, a.message.padding_bytes)


def test_what_is_not_tag_data_is_refused():
    good = mac.pack_tag_data(data())
    for bad in (b"", good[:-1], good + b"\0", b"\2" + good[1:], good[:1] + b"\0" + good[2:]):
        assert mac.node_msg_type(bad) is None
        with pytest.raises(ValueError):
            mac.parse_tag_data(bad)
    with pytest.raises(ValueError):
        mac.pack_tag_data(data(after=6))


# ----------------------------------------------------------------------------------------------
# The sink
# ----------------------------------------------------------------------------------------------

def test_dbfs_rounds_as_c_does():
    assert sink.c_round(2.5) == 3.0 and sink.c_round(-2.5) == -3.0
    assert sink.c_round(0.49999999999999994) == 0.0 and sink.c_round(-0.5) == -1.0
    assert sink.dbfs(sink.FULL_SCALE2) == 0.0
    assert sink.dbfs(0.0) == -999.0
    assert sink.dbfs(sink.FULL_SCALE2 / 10) == -10.0


def test_rows_have_the_reader_rx_columns_and_formats():
    assert sink.COLUMNS == ("time_ms", "after", "kind", "node_id", "value", "id", "r_sfs", "slot", "rssi_dbfs")
    assert sink.row(msg1()) == (1062.0, "paging_cbra", "msg1", -1, -1, 0xBEEF, 2, 1, sink.dbfs(1.0e3))
    assert sink.row(data()) == (1080.0166666666667, "random_id_response", "data", 3, 42, 7, 4, 0, sink.dbfs(2.5e2))
    assert sink.row(data(as_id=None))[5] == ""


def test_the_sink_writes_each_message_and_counts_the_lost(tmp_path):
    s = sink.Sink(str(tmp_path), "127.0.0.1", 0)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sock.settimeout(2)
        for d in (mac.pack_tag_data(msg1(seq=0)), b"not Tag data", mac.pack_tag_data(data(seq=1)),
                  mac.pack_tag_data(data(seq=4, node=5))):
            tx.sendto(d, s.address)
            s.take(s.sock.recv(mac.NODE_MSG_MAX_BYTES + 1))
    finally:
        tx.close()
        s.close()
    assert (s.received, s.lost, s.refused) == (3, 2, 1)
    with open(os.path.join(tmp_path, "tag_data.csv"), newline="") as f:
        text = f.read()
    rows = list(csv.reader(text.splitlines()))
    assert text.endswith("\r\n")
    assert rows[0] == list(sink.COLUMNS)
    assert rows[1][:8] == ["1062.0", "paging_cbra", "msg1", "-1", "-1", str(0xBEEF), "2", "1"]
    assert [r[3] for r in rows[2:]] == ["3", "5"]
    assert float(rows[1][8]) == sink.dbfs(1.0e3)
