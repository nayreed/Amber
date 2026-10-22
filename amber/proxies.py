# amber/proxies.py
"""Stand-ins for the object on the other side, used by the Reader and Tag hosts (amber.hosts).

The Reader and the Tags run in separate hosts (amber.hosts). BSBehavior calls
BackscatterModule.receive_command() on a TagProxy, and BackscatterModule.do_transmit() writes
three fields into a BSProxy and interrupts it. These proxies record those calls so that amber.iq
can turn them into waveforms: a command becomes bits on the Reader's carrier, and a transmission
becomes the Tag's reflection of it.
"""

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class Delivery:
    """One BackscatterModule.receive_command() call made by a Reader."""
    node_id: int
    cmd: str
    bs_id: int
    data: Dict[str, Any]


@dataclass
class Uplink:
    """One BSBehavior interrupt made by a Tag, with the fields it set before interrupting.
    rssi_dbm is the power the Reader's best sector receives from the Tag, which sets the
    amplitude of the Tag's reflection."""
    bs_id: int
    node_id: int
    payload: Any
    rssi_dbm: float


class _NodeId:
    def __init__(self, node_id: int):
        self.id = node_id


class TagProxy:
    """Reader-side stand-in for a remote BackscatterModule. BSBehavior only uses .node.id and
    receive_command() on its backscatter_modules."""

    def __init__(self, node_id: int, outbox: List[Delivery]):
        self.node = _NodeId(node_id)
        self._outbox = outbox

    def receive_command(self, cmd: str, bs_id: int, data: dict = None):
        self._outbox.append(Delivery(self.node.id, cmd, bs_id, data or {}))


class _InterruptRecorder:
    def __init__(self, bs: "BSProxy"):
        self._bs = bs

    def interrupt(self, cause=None):
        assert cause == "receive_data", f"unexpected interrupt cause {cause!r} from a Tag"
        bs = self._bs
        bs._outbox.append(Uplink(bs.id, bs.received_tag_id, bs.data_from_tag, bs.rssi_dbm))


class BSProxy:
    """Tag-side stand-in for a remote BSBehavior. BackscatterModule.do_transmit() only uses .id,
    the three scratch fields and action.interrupt("receive_data")."""

    def __init__(self, bs_id: int, outbox: List[Uplink]):
        self.id = bs_id
        self._outbox = outbox
        self.received_tag_id = 0
        self.data_from_tag = 0
        self.rssi_dbm = -999.0
        self.action = _InterruptRecorder(self)
