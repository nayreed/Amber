# amber/protocols/adaptive_aloha.py
"""Adaptive framed ALOHA: the number of access occasions follows the load.

Each frame is a CBRA paging with a new Transaction ID offering n = 2^v access occasions, and the
Access Triggers that play them out; every Msg1 decoded is answered with a Random ID Response,
which brings that Tag's data. After each frame the Reader counts the Msg1s it decoded and the
collisions, smooths that load (load = (1 - smoothing) * load + smoothing * (heard + collided)),
and moves v one step towards log2(occasions_per_contender * load), within [min_exp, max_exp].
v starts at start_exp.

The Reader keeps (time in s, n, heard, collided) for every frame; a run writes it to
adaptation.csv.
"""

import math

from ..mac import pdu as mp
from .base import Protocol


class AdaptiveAloha(Protocol):
    PARAMS = {"start_exp": 4, "min_exp": 1, "max_exp": 6, "smoothing": 0.25, "occasions_per_contender": 2}

    def check(self):
        self.within("min_exp", 0, 15)
        self.within("max_exp", self.params["min_exp"], 15)
        self.within("start_exp", self.params["min_exp"], self.params["max_exp"])
        self.positive("occasions_per_contender")
        if not 0 < self.params["smoothing"] <= 1:
            raise ValueError(f"protocol {self.name}: smoothing must be in (0, 1], got {self.params['smoothing']}")

    def policy(self, ctx):
        p = self.params
        v, load = p["start_exp"], 0.0
        sched = ctx.cfg.cbra()
        self.history = []
        while True:
            heard = collided = 0
            for i in range(self.r2d_per_paging(ctx, v, sched)):
                msg = ctx.mac.paging_cbra(ctx.next_tid(), sched, n_occasions_code=v) if i == 0 else mp.AccessTrigger()
                h1, _ = yield from self.access(ctx, msg)
                heard += sum(1 for a in h1.answers if a.kind == "msg1")
                collided += h1.collided
            self.history.append((round(ctx.now_ms / 1000.0, 6), 1 << v, heard, collided))
            load = (1 - p["smoothing"]) * load + p["smoothing"] * (heard + collided)
            target = max(1.0, load * p["occasions_per_contender"])
            target_v = min(p["max_exp"], max(p["min_exp"], math.ceil(math.log2(target))))
            v += (target_v > v) - (target_v < v)

    def outputs(self):
        return {"adaptation.csv": (("time_s", "n_occasions", "heard", "collided"), list(getattr(self, "history", [])))}


PROTOCOL = AdaptiveAloha
