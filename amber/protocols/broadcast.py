# amber/protocols/broadcast.py
"""Broadcast: page every Tag, offer a few access occasions, read everyone heard.

Each frame is a CBRA paging with a new Transaction ID, so every Tag answers again, offering
2^occasions_exp access occasions (spread over the paging and the Access Triggers that follow,
m = x * N_SFS per R2D). Every Tag that hears it and has energy sends a Msg1 on a random occasion;
the Reader answers every Msg1 it decoded with a Random ID Response, which brings their data. Two
Tags on the same occasion and R_SFS collide; with SIC on, the stronger may still be decoded.
"""

from ..mac import pdu as mp
from .base import Protocol


class Broadcast(Protocol):
    PARAMS = {"occasions_exp": 2}

    def check(self):
        self.within("occasions_exp", 0, 15)

    def policy(self, ctx):
        v = self.params["occasions_exp"]
        sched = ctx.cfg.cbra()
        while True:
            yield from self.access(ctx, ctx.mac.paging_cbra(ctx.next_tid(), sched, n_occasions_code=v))
            for _ in range(self.r2d_per_paging(ctx, v, sched) - 1):
                yield from self.access(ctx, mp.AccessTrigger())


PROTOCOL = Broadcast
