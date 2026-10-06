"""
Illustrative re-creation of the discovery-phase experiment of the Amber demo
abstract (node density vs. successful registration), using Amber's *default*
registration protocol (send_id -> random slot -> ack).

This is NOT the authors' exact configuration: the abstract does not state the
frame length, number of discovery rounds or timing.  What we keep from the
abstract is: 924 MHz, 30 dBm / 17 dBi sectorised BS, 1 dBi omni passive nodes
in hybrid power mode, nodes uniformly placed within 100 m of the BS, three
subcarrier shifts assigned to the nodes.  Everything else is our own choice
and is stated in the textbook.

Output: JSON with, for each node count N and number of discovery rounds r,
the list (over runs) of the registered fraction after r rounds.
"""
import json
import math
import os
import random
import sys
from multiprocessing import Pool

import simpy

from amber import (backscatter, bsengine, capacitor, controller, energy,
                   propagation, radiodevices)

HERE = os.path.dirname(os.path.abspath(__file__))
TRACE = os.path.join(HERE, "..", "..", "demo_experiments",
                     "threehundred_seconds_stable.xlsx")

FREQ = 924e6
WARMUP_MS = 4_000          # let every node finish its startup delay + charge
N_SLOTS = 10               # reply slots per discovery round
SLOT_MS = 8
CMD_MS = 5
ROUNDS = 4
SHIFTS = 3


def one_run(args):
    n_nodes, seed, enable_sic = args
    random.seed(seed)
    env = simpy.Environment()
    src = energy.EnvEnergySource(env=env, file_path=TRACE, column="V_IM",
                                 resistance=5e3)

    nodes = []
    for i in range(n_nodes):
        r = 100.0 * math.sqrt(random.random())          # uniform in a disc
        a = random.uniform(0, 2 * math.pi)
        nodes.append(radiodevices.Node(
            id=i, x=r * math.cos(a), y=r * math.sin(a), height=1.5,
            antenna_gain_dbi=1.0, sensitivity_dbm=-45.0, efficiency=0.7,
            subcarrier_shift=i % SHIFTS))
    bs = radiodevices.BaseStation(
        id=0, x=0, y=0,
        sectors=[radiodevices.Sector(azimuth_deg=a, beamwidth_deg=65, power=30,
                                     antenna_gain_dbi=17, sensitivity_dbm=-100,
                                     height=25) for a in (0, 120, 240)])

    cov = propagation.CoverageMap(
        base_stations=[bs], nodes=nodes, freq_hz=FREQ, pathloss_model="macro",
        los=True, node_energy_mode="hybrid",
        node_ext_power_fn=lambda n: src.ext_power, combine_mode="max")
    dl = cov.compute_bs_to_point(nodes)
    cov.calculate_node_power(nodes, dl)
    ul = cov.compute_point_to_bs(nodes)

    cparams = capacitor.CapacitorParams()
    ctl = controller.ControllerParams(
        currents=controller.CurrentsA(1.4e-4, 0.512e-3, 1.28e-3, 5e-3),
        durations_ms=controller.DurationsMs(5, 2, 5, 15),
        thresholds_v=controller.VoltageThresholdsV(high=1.7, low=1.3))

    bsms = []
    for node in nodes:
        cap = capacitor.Capacitor(env=env, id=node.id, params=cparams,
                                  keep_logs=False)
        bsm = backscatter.BackscatterModule(
            env=env, node=node, bs_processes=[], uplink_results=ul,
            downlink_results=dl, keep_logs=False)
        controller.Controller(env=env, capacitor_ctrl=cap, node=node,
                              backscatter=bsm, params=ctl, coverage_map=cov,
                              downlink_results=dl, keep_logs=False)
        bsms.append(bsm)

    registered_after_round = []

    def policy(b):
        yield [("rx", WARMUP_MS, "warmup")]                  # nodes charge up
        for _ in range(ROUNDS):
            yield ([("tx", CMD_MS, "send_id", {"cmd": "send_id", "target": -1})]
                   + [("rx", SLOT_MS, "slot") for _ in range(N_SLOTS)]
                   + [("tx", CMD_MS, "ack", {"cmd": "ack"})])
            registered_after_round.append(
                sum(1 for m in bsms if m.state == "registered"))
        # Keep the BS process alive: the two-frame RX look-ahead can leave a
        # node holding a slot in a "next frame" that never comes, and a node
        # backscattering into a terminated BS process raises RuntimeError.
        yield [("rx", 10_000, "tail")]

    beh = bsengine.BSBehavior(env=env, base_station=bs, policy=policy,
                              backscatter_modules=bsms, enable_sic=enable_sic,
                              keep_logs=False)
    for m in bsms:
        m.bs_processes = [beh]

    horizon = WARMUP_MS + ROUNDS * (2 * CMD_MS + N_SLOTS * SLOT_MS) + 50
    env.run(until=horizon)
    return n_nodes, enable_sic, [c / n_nodes for c in registered_after_round]


if __name__ == "__main__":
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    out = sys.argv[2] if len(sys.argv) > 2 else "registration_results.json"
    densities = [5, 10, 20, 30, 40, 60, 80, 100]
    jobs = [(n, 1000 * n + s, sic) for n in densities for s in range(runs)
            for sic in (False, True)]
    with Pool(4) as pool:
        res = pool.map(one_run, jobs, chunksize=1)
    table = {}
    for n, sic, fr in res:
        table.setdefault(f"{'sic' if sic else 'nosic'}:{n}", []).append(fr)
    with open(out, "w") as f:
        json.dump({"slots": N_SLOTS, "shifts": SHIFTS, "rounds": ROUNDS,
                   "runs": runs, "results": table}, f)
    for k in sorted(table, key=lambda s: (s.split(':')[0], int(s.split(':')[1]))):
        arr = table[k]
        mean = [sum(r[i] for r in arr) / len(arr) for i in range(ROUNDS)]
        print(k, " ".join(f"{100*m:5.1f}" for m in mean))
