# amber/protocols/registration.py
"""Registration: Tags join once (CBRA, one Transaction ID), then the Reader polls each by AS ID.

The Reader keeps one Transaction ID for the whole run, so a Tag that has joined ignores later
pagings (38.391 §5.2), and a Tag whose access failed (its Msg1 collided, or it got no Random ID
Response) tries again at the next paging (§5.5.3). Each frame is:

1. Join: a CBRA paging offering 2^occasions_exp access occasions and its Access Triggers; every
   Msg1 decoded is answered with a Random ID Response assigning an AS ID, which brings that Tag's
   data and registers it.
2. Poll: an R2D Upper Layer Data Transfer to every registered AS ID (Received Data Size 0), which
   asks that Tag alone for data.
"""

from ..mac import pdu as mp
from .base import Protocol


class Registration(Protocol):
    PARAMS = {"occasions_exp": 2}

    def check(self):
        self.within("occasions_exp", 0, 15)

    def policy(self, ctx):
        v = self.params["occasions_exp"]
        sched = ctx.cfg.cbra()
        tid = ctx.next_tid()
        registered = []                 # AS IDs, in the order they joined
        while True:
            for i in range(self.r2d_per_paging(ctx, v, sched)):
                msg = ctx.mac.paging_cbra(tid, sched, n_occasions_code=v) if i == 0 else mp.AccessTrigger()
                _, h2 = yield from self.access(ctx, msg)
                for a in h2.answers if h2 else []:
                    if a.as_id not in registered:
                        registered.append(a.as_id)
            for as_id in registered:
                yield ctx.mac.data(as_id, ctx.cfg.unicast(), received=0)


PROTOCOL = Registration
