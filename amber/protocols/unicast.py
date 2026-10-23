# amber/protocols/unicast.py
"""Unicast: ask each Tag for data in turn, with a contention-free paging.

Each frame is, for every Tag in the scenario's order, an A-IoT Paging for CFA carrying that Tag's
Paging ID (its node id on 16 bits). Only that Tag answers, with its data, so there are no
collisions between Tags.
"""

from .base import Protocol


class Unicast(Protocol):
    PARAMS = {}

    def policy(self, ctx):
        from ..iq import paging_id
        sched = ctx.cfg.cfa()
        while True:
            for node in self.node_ids:
                yield ctx.mac.paging_cfa(paging_id(node), sched)


PROTOCOL = Unicast
