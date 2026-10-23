"""Rel-19 A-IoT MAC procedures, TS 38.391 v19.0.0 clause 5: the device side, and the Reader's part.

The device MAC (DeviceMAC) follows clause 5: paging (5.2), contention-based access
with the access-occasion count-down (5.3.1), contention-free access (5.3.2), upper layer data and
segmentation (5.4) and failure detection (5.5). It never touches the air: each R2D message goes in
through on_r2d(), and what the device must send comes back as D2RSend actions, with the L1
parameters and the timing context the PHY needs (params.t_r_to_d).

The Reader side (ReaderMAC) builds the R2D messages and turns what the Reader receives back into
the next messages. 38.391 says what the device does; what the Reader does (how many occasions, when
to send Access Trigger, which AS ID to assign, when to NACK) is left to the Reader, so that is where
custom protocols live: a protocol is a Reader policy that only emits 38.391 messages.

The readings of the spec this module depends on, where 38.391 leaves room:
- Access occasion numbering. 5.3.1.1 selects "the (counter+1)th of the m = X * N_SFS occasions"
  but does not order them. Here occasion k is time slot k // N_SFS on R_SFS value k % N_SFS of the
  ascending set. The Reader listens on every R_SFS in every slot, so it does not need to know the
  order: it sees (slot, R_SFS) directly.
- Msg2 matching, 5.3.1.3. The text joins "Echoed Random ID matches" and "Frequency Index present and
  matching" with "and". Read literally, a Random ID Response without Frequency Index (FIPI = 0) would
  never match, yet the spec defines FIPI = 0. Read here as: the Echoed Random ID matches, and, when
  the Frequency Index is present, it also matches the R_SFS used for Msg1.
- R_SFS of the D2R after Msg2: the i-th entry's device takes the i-th value of the Random ID
  Response's bitmap (Table 6.2.1.6-1, derived-parameter column).
- 5.5.3 is checked before 5.2 when a paging arrives, so that a failed CBRA lets the same
  Transaction ID start access again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

from . import pdu as mp
from ..phy.params import D2RParams

SDU_HEADER_BYTES = 2      # D2R Upper Layer Data Transfer: type, R, MDI, SDU Length
MAX_SDU_BYTES = 127       # 7-bit SDU Length


@dataclass
class D2RSend:
    """One D2R transmission the device's MAC asks the PHY for.

    msg is the kind params.t_r_to_d needs ("msg1", "msg2", "cfa" or "data"). slot is the access
    occasion's time slot (Msg1 only, 0 or 1); earlier_occasion is True when an earlier occasion
    follows the same R2D. candidate_r_sfs is the set T'_chip is taken over (Msg1 and Msg2)."""
    pdu: List[int]
    params: D2RParams
    msg: str
    tbs_bytes: int
    message: object
    slot: int = 0
    candidate_r_sfs: Tuple[int, ...] = ()

    @property
    def earlier_occasion(self) -> bool:
        return self.slot > 0


class UpperLayer:
    """What the device's upper layer gives the MAC. Override any of these; the defaults answer
    every Paging ID and have nothing to send."""

    def paging_id_matched(self, paging_id: Optional[Sequence[int]]) -> bool:
        return True

    def selected(self) -> None:
        """5.2: told when a paging without Paging ID selects the device."""

    def deliver(self, sdu: bytes) -> None:
        """An R2D Data SDU for this device (5.4.3)."""

    def next_sdu(self) -> Optional[bytes]:
        """Upper layer data to send now, or None (5.4.2)."""
        return None


class DeviceMAC:
    """The A-IoT MAC entity of one device (38.391 clause 5).

    on_r2d(message) handles one received R2D message and returns the D2R transmissions it triggers
    (zero or one). modulation is the device's own choice (38.291 §7.1.3); rng draws the access
    occasion and the 16-bit Random ID."""

    def __init__(self, upper: Optional[UpperLayer] = None, rng: Optional[np.random.Generator] = None,
                 modulation: str = "bpsk"):
        self.upper = upper or UpperLayer()
        self.rng = rng or np.random.default_rng()
        self.modulation = modulation
        # stored state, named as in the spec
        self.transaction_id: Optional[int] = None
        self.as_id: Optional[int] = None
        self.failed_tid: Optional[int] = None       # Transaction ID whose procedure was found failed
        # CBRA in progress
        self.cbra_active = False
        self.cbra_success = False
        self.counter: Optional[int] = None          # ACCESS_OCCASION_COUNTER, None once Msg1 is sent
        self.cbra_sched: Optional[mp.SchedulingInfo] = None
        self.random_id: Optional[int] = None
        self.msg1_r_sfs: Optional[int] = None
        self.msg2_budget = 0                        # Access Trigger / Paging still allowed while waiting Msg2
        self.k = 1
        # CFA and data
        self.cfa_in_procedure = False
        self.nack_monitoring = False
        self.sending: Optional[bytes] = None        # the SDU being sent, kept while segmented
        self.log: List[str] = []

    # ------------------------------------------------------------------------------------------
    def on_r2d(self, m) -> List[D2RSend]:
        if isinstance(m, (mp.AIoTPagingCBRA, mp.AIoTPagingCFA)):
            return self._paging(m)
        if isinstance(m, mp.AccessTrigger):
            return self._access_trigger()
        if isinstance(m, mp.RandomIDResponse):
            return self._random_id_response(m)
        if isinstance(m, mp.R2DUpperLayerDataTransfer):
            return self._r2d_data(m)
        if isinstance(m, mp.NACKFeedback):
            self._nack(m)
            return []
        raise TypeError(f"not an R2D message: {m!r}")

    # -- 5.2 paging, with 5.5.3 first ---------------------------------------------------------
    def _paging(self, m) -> List[D2RSend]:
        # 5.5.3: a paging while a CBRA has not succeeded marks the stored Transaction ID failed
        if self.cbra_active and not self.cbra_success:
            self.failed_tid = self.transaction_id
            self.log.append(f"5.5.3 CBRA for TID {self.transaction_id} failed")
        self._stop_cbra()
        self.nack_monitoring = False        # 5.5.2: NACK monitoring ends at a paging
        self.msg2_budget = 0

        if isinstance(m, mp.AIoTPagingCBRA):
            tid = m.transaction_id
            fresh = (self.transaction_id is None or tid != self.transaction_id
                     or (tid == self.transaction_id and self.failed_tid == tid))
            if not fresh:
                self.log.append(f"5.2 paging TID {tid} ignored: already done")
                return []
            self.as_id = None
            self.transaction_id = tid
            self.failed_tid = None
            self.cfa_in_procedure = False
            if m.paging_id is None:
                self.upper.selected()
                selected = True
            else:
                selected = self.upper.paging_id_matched(m.paging_id)
            if not selected:
                return []
            return self._start_cbra(m)

        # CFA
        self.as_id = None
        self.transaction_id = None
        self.failed_tid = None
        if not self.upper.paging_id_matched(m.paging_id):
            self.cfa_in_procedure = False
            return []
        self.cfa_in_procedure = True
        self.log.append("5.3.2 CFA")
        return self._d2r_data(m.sched, "cfa")

    # -- 5.3.1 CBRA ---------------------------------------------------------------------------
    def _start_cbra(self, m: mp.AIoTPagingCBRA) -> List[D2RSend]:
        self.cbra_active, self.cbra_success = True, False
        self.cbra_sched = m.sched
        self.k = m.k
        n = m.num_access_occasions
        self.counter = int(self.rng.integers(0, n))
        self.log.append(f"5.3.1.1 n={n} counter={self.counter}")
        return self._try_occasion()

    def _occasions(self) -> int:
        return self.cbra_sched.x * len(self.cbra_sched.r_sfs)

    def _try_occasion(self) -> List[D2RSend]:
        m = self._occasions()
        if self.counter >= m:
            return []
        k = self.counter
        n_sfs = len(self.cbra_sched.r_sfs)
        slot, r_sfs = k // n_sfs, self.cbra_sched.r_sfs[k % n_sfs]
        self.counter = None
        # 5.3.1.2
        self.random_id = int(self.rng.integers(0, 1 << 16))
        self.msg1_r_sfs = r_sfs
        self.msg2_budget = self.k
        msg = mp.AccessRandomID(self.random_id)
        p = mp.to_d2r_params(self.cbra_sched, r_sfs, self.modulation)
        self.log.append(f"5.3.1.2 Msg1 id={self.random_id:#06x} slot={slot} R_SFS={r_sfs}")
        return [D2RSend(msg.pack(), p, "msg1", 2, msg, slot, tuple(self.cbra_sched.r_sfs))]

    def _access_trigger(self) -> List[D2RSend]:
        if self.cbra_active and self.counter is None and self.msg2_budget > 0 and not self.cbra_success:
            self.msg2_budget -= 1           # 5.3.1.3: Msg2 is monitored until K triggers or pagings
        if self.cbra_active and self.counter is not None:
            self.counter -= self._occasions()
            return self._try_occasion()
        return []

    def _stop_cbra(self):
        self.cbra_active = False
        self.counter = None

    # -- 5.3.1.3 Random ID Response ----------------------------------------------------------
    def _random_id_response(self, m: mp.RandomIDResponse) -> List[D2RSend]:
        if self.as_id is None:
            if not self.cbra_active or self.counter is not None or self.msg2_budget <= 0:
                return []
            for i, e in enumerate(m.entries):
                if e.echoed_random_id != self.random_id:
                    continue
                if e.freq_index is not None and e.r_sfs != self.msg1_r_sfs:
                    continue
                self.cbra_success = True
                self.as_id = e.assigned_as_id if e.assigned_as_id is not None else e.echoed_random_id
                self.log.append(f"5.3.1.3 Msg2 matched entry {i}, AS ID {self.as_id:#06x}")
                return self._after_msg2(m, i)
            return []
        for i, e in enumerate(m.entries):
            mine = (e.assigned_as_id == self.as_id) if e.assigned_as_id is not None \
                else (e.echoed_random_id == self.as_id)
            if mine:
                return self._after_msg2(m, i)
        return []

    def _after_msg2(self, m: mp.RandomIDResponse, i: int) -> List[D2RSend]:
        if i >= len(m.sched.r_sfs):
            raise ValueError(f"Random ID Response entry {i} has no R_SFS: the bitmap indicates {m.sched.r_sfs}")
        sends = self._d2r_data(m.sched, "msg2", r_sfs=m.sched.r_sfs[i])
        self.nack_monitoring = True         # 5.5.2: the first data after CBRA
        return sends

    # -- 5.4.3 R2D data -------------------------------------------------------------------------
    def _r2d_data(self, m: mp.R2DUpperLayerDataTransfer) -> List[D2RSend]:
        if self.as_id is not None and m.as_id == self.as_id:
            self.nack_monitoring = False    # 5.5.2: monitoring ends at R2D data addressed to the device
            if m.ci == 1:
                self.upper.deliver(m.data_sdu)
                return self._d2r_data(m.sched, "data")
            if m.received_data_size == 0:
                return self._d2r_data(m.sched, "data")
            return self._segment(m.sched, m.received_data_size)
        if self.as_id is None and self.cfa_in_procedure and m.ci == 1:
            self.as_id = m.as_id
            self.upper.deliver(m.data_sdu)
            return self._d2r_data(m.sched, "data")
        return []

    # -- 5.4.2 D2R message transmission, 5.4.4 segmentation ------------------------------------
    def _d2r_data(self, sched: mp.SchedulingInfo, msg: str, r_sfs: Optional[int] = None) -> List[D2RSend]:
        sdu = self.upper.next_sdu()
        self.sending = None
        if sdu is not None and SDU_HEADER_BYTES + len(sdu) > sched.d2r_tbs:
            self.sending = bytes(sdu)
            return self._segment(sched, 0, msg, r_sfs)
        m = mp.D2RUpperLayerDataTransfer.for_tbs(bytes(sdu or b""), sched.d2r_tbs, mdi=0)
        return [self._send(m, sched, msg, r_sfs)]

    def _segment(self, sched: mp.SchedulingInfo, x: int, msg: str = "data",
                 r_sfs: Optional[int] = None) -> List[D2RSend]:
        if self.sending is None:
            return []                       # nothing was being segmented: nothing to continue
        room = min(sched.d2r_tbs - SDU_HEADER_BYTES, MAX_SDU_BYTES)
        if room <= 0:
            raise ValueError(f"D2R TBS {sched.d2r_tbs} leaves no room for a segment")
        part = self.sending[x:x + room]
        last = x + room >= len(self.sending)
        m = mp.D2RUpperLayerDataTransfer.for_tbs(part, sched.d2r_tbs, mdi=0 if last else 1)
        if last:
            self.sending = None
        self.log.append(f"5.4.4 segment from byte {x}, {len(part)} bytes, MDI {0 if last else 1}")
        return [self._send(m, sched, msg, r_sfs)]

    def _send(self, m, sched, msg, r_sfs) -> D2RSend:
        if r_sfs is None:
            r_sfs = sched.r_sfs[0]
        p = mp.to_d2r_params(sched, r_sfs, self.modulation)
        cand = tuple(sched.r_sfs) if msg == "msg2" else ()
        return D2RSend(m.pack(), p, msg, sched.d2r_tbs, m, 0, cand)

    # -- 5.5.2 NACK -----------------------------------------------------------------------------
    def _nack(self, m: mp.NACKFeedback):
        if not self.nack_monitoring or self.as_id is None:
            return
        if self.as_id in m.as_ids:
            self.log.append(f"5.5.2 NACK for AS ID {self.as_id:#06x}: TID {self.transaction_id} failed")
            self.as_id = None
            self.failed_tid = self.transaction_id
            self.nack_monitoring = False


# ----------------------------------------------------------------------------------------------
# Reader side
# ----------------------------------------------------------------------------------------------

@dataclass
class Msg1Heard:
    """A Msg1 the Reader decoded: its Random ID, the R_SFS and time slot it came on."""
    random_id: int
    r_sfs: int
    slot: int = 0


@dataclass
class ReaderMAC:
    """Builds the Reader's 38.391 messages. Holds the AS IDs it gave out; everything about when
    to send what is the caller's policy (tests/test_mac_procedures.py runs a whole inventory with it)."""
    next_as_id: int = 1
    assigned: dict = field(default_factory=dict)       # AS ID -> Random ID it came from

    @staticmethod
    def sched(variant: str, r_sfs: Sequence[int], d2r_tbs: Optional[int] = None, x: Optional[int] = None,
              **kw) -> mp.SchedulingInfo:
        return mp.SchedulingInfo(variant=variant, r_sfs=tuple(sorted(r_sfs)), d2r_tbs=d2r_tbs, x=x, **kw)

    def paging_cbra(self, tid: int, sched: mp.SchedulingInfo, n_occasions_code: int = 0, k_bit: int = 0,
                    paging_id=None) -> mp.AIoTPagingCBRA:
        return mp.AIoTPagingCBRA(transaction_id=tid, sched=sched, paging_id=paging_id,
                                 num_access_occasions_code=n_occasions_code, k_bit=k_bit)

    def paging_cfa(self, paging_id, sched: mp.SchedulingInfo) -> mp.AIoTPagingCFA:
        return mp.AIoTPagingCFA(paging_id=paging_id, sched=sched)

    def random_id_response(self, heard: Sequence[Msg1Heard], sched: mp.SchedulingInfo,
                           assign: bool = True, with_freq: bool = True) -> mp.RandomIDResponse:
        """Answer up to 8 Msg1s. The i-th listed device sends its D2R on the i-th R_SFS of sched,
        so sched must indicate at least as many R_SFS values as there are entries."""
        heard = list(heard)[:mp.MAX_ID_ENTRIES]
        if len(sched.r_sfs) < len(heard):
            raise ValueError(f"{len(heard)} entries need {len(heard)} R_SFS values, the bitmap has {sched.r_sfs}")
        entries = []
        for h in heard:
            as_id = None
            if assign:
                as_id = self.next_as_id
                self.assigned[as_id] = h.random_id
                self.next_as_id = (self.next_as_id + 1) & 0xFFFF or 1
            fi = mp.R_SFS_VALUES.index(h.r_sfs) if with_freq else None
            entries.append(mp.RandomIDEntry(h.random_id, as_id, fi))
        return mp.RandomIDResponse(sched=sched, entries=entries)

    @staticmethod
    def as_id_of(entry: mp.RandomIDEntry) -> int:
        return entry.assigned_as_id if entry.assigned_as_id is not None else entry.echoed_random_id

    def data(self, as_id: int, sched: mp.SchedulingInfo, sdu: Optional[bytes] = None,
             received: Optional[int] = None) -> mp.R2DUpperLayerDataTransfer:
        return mp.R2DUpperLayerDataTransfer(as_id=as_id, sched=sched, data_sdu=sdu, received_data_size=received)

    @staticmethod
    def nack(as_ids: Sequence[int]) -> mp.NACKFeedback:
        return mp.NACKFeedback(as_ids=list(as_ids))


@dataclass
class Reassembly:
    """The Reader's side of D2R segmentation (5.4.4): collects segments per AS ID."""
    parts: dict = field(default_factory=dict)

    def add(self, as_id: int, m: mp.D2RUpperLayerDataTransfer) -> Tuple[Optional[bytes], int]:
        """Returns (the whole SDU when this was the last segment, else None; bytes received so far)."""
        buf = self.parts.setdefault(as_id, bytearray())
        buf += m.sdu
        if m.mdi == 0:
            del self.parts[as_id]
            return bytes(buf), len(buf)
        return None, len(buf)


def occasion_of(sched: mp.SchedulingInfo, k: int) -> Tuple[int, int]:
    """(time slot, R_SFS) of access occasion k (0-based) of one Paging or Access Trigger. See the
    module docstring for why the order does not matter to the Reader."""
    n = len(sched.r_sfs)
    return k // n, sched.r_sfs[k % n]

