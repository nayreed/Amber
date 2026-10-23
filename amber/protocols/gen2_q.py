# amber/protocols/gen2_q.py
"""EPC Gen2's Q algorithm on 38.391: the Reader decides after every access occasion.

A round is a CBRA paging with a new Transaction ID offering 2^Q access occasions on a single
occasion per R2D (one time slot, one R_SFS: m = 1), then 2^Q - 1 Access Triggers. That is Gen2
exactly: the paging is Query (each Tag draws its counter from 0..2^Q - 1, 38.391 §5.3.1.1), an
Access Trigger is QueryRep (every counter steps down), and the Tag whose counter reaches its
occasion sends its Msg1 (Gen2's RN16). A decoded Msg1 is answered with a Random ID Response
(Gen2's ACK), which brings the Tag's data.

After every occasion the Reader updates Qf (starting at q0): + c after a collision, - c after an
empty occasion, unchanged after a success, within [0, 15]. The next round uses Q = round(Qf).

The Reader keeps (time in s, Q, decoded, collided) for every occasion; a run writes it to q.csv.
"""

from ..mac import pdu as mp
from .base import Protocol


class Gen2Q(Protocol):
    PARAMS = {"q0": 4.0, "c": 0.3}

    def check(self):
        p = self.params
        if not 0 <= p["q0"] <= 15:
            raise ValueError(f"protocol {self.name}: q0 must be within [0, 15], got {p['q0']}")
        if not 0 < p["c"] <= 1:
            raise ValueError(f"protocol {self.name}: c must be in (0, 1], got {p['c']}")

    def policy(self, ctx):
        p = self.params
        qf = float(p["q0"])
        sched = ctx.cfg.cbra(r_sfs=ctx.cfg.r_sfs[:1], x=1)     # one occasion per R2D, as Gen2's slots
        self.history = []
        while True:
            q = round(qf)
            for i in range(1 << q):
                msg = ctx.mac.paging_cbra(ctx.next_tid(), sched, n_occasions_code=q) if i == 0 else mp.AccessTrigger()
                h1, _ = yield from self.access(ctx, msg)
                decoded = sum(1 for a in h1.answers if a.kind == "msg1")
                if h1.collided or decoded > 1:
                    qf = min(15.0, qf + p["c"])
                elif not decoded:
                    qf = max(0.0, qf - p["c"])
                self.history.append((round(ctx.now_ms / 1000.0, 6), q, decoded, h1.collided))

    def outputs(self):
        return {"q.csv": (("time_s", "q", "decoded", "collided"), list(getattr(self, "history", [])))}


PROTOCOL = Gen2Q
