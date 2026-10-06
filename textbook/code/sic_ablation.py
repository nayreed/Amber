"""
Controlled ablation of the two shipped broadcast demos.

The shipped `broadcast` and `broadcast_sic` demos differ in FIVE parameters at
once (capacitance, RX slot length, number of slots, sector beamwidth, sector
sensitivity) *and* in enable_sic.  This script rebuilds the scenario with a
single function so each factor can be varied independently, and repeats every
configuration over several random seeds.

Metric (as in the demos): DER = 1 - decoded/sent, throughput = decoded / s.
"""
import json
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
SIM_MS = 20_001


def run(cfg, seed):
    random.seed(seed)
    env = simpy.Environment()
    src = energy.EnvEnergySource(
        env=env, file_path=TRACE, column="V_IM", resistance=5e3)
    nodes = [radiodevices.Node(id=i, x=0, y=y, height=1.5,
                               sensitivity_dbm=-100, efficiency=0.7)
             for i, y in enumerate((20, 30, 100))]
    wpt, hyb = nodes[:2], nodes[2:]
    bs = radiodevices.BaseStation(id=0, x=0, y=0, sectors=[
        radiodevices.Sector(azimuth_deg=a, beamwidth_deg=cfg["beamwidth"],
                            power=46, antenna_gain_dbi=15,
                            sensitivity_dbm=cfg["sector_sens"], height=25)
        for a in (0, 120, 240)])

    def cmap(ns, mode):
        return propagation.CoverageMap(
            base_stations=[bs], nodes=ns, freq_hz=924e6, pathloss_model="macro",
            los=True, node_energy_mode=mode,
            node_ext_power_fn=lambda n: src.ext_power, combine_mode="max")
    cw, ch = cmap(wpt, "wpt"), cmap(hyb, "hybrid")
    parts = []
    for cov, ns in ((cw, wpt), (ch, hyb)):
        dl = cov.compute_bs_to_point(ns)
        cov.calculate_node_power(ns, dl)
        ul = cov.compute_point_to_bs(ns)
        parts.append((cov, ns, dl, ul))

    ctl = controller.ControllerParams(
        currents=controller.CurrentsA(1.4e-4, 0.512e-3, 1.28e-3, 5e-3),
        durations_ms=controller.DurationsMs(5, 2, 5, 15),
        thresholds_v=controller.VoltageThresholdsV(high=1.7, low=1.3))
    cp = capacitor.CapacitorParams(C=cfg["C"])
    bsms = []
    for cov, ns, dl, ul in parts:
        for node in ns:
            cap = capacitor.Capacitor(env=env, id=node.id, params=cp,
                                      keep_logs=False)
            m = backscatter.BackscatterModule(
                env=env, node=node, bs_processes=[], uplink_results=ul,
                downlink_results=dl, keep_logs=True)
            m.state = "registered"
            controller.Controller(env=env, capacitor_ctrl=cap, node=node,
                                  backscatter=m, params=ctl, coverage_map=cov,
                                  downlink_results=dl, keep_logs=False)
            bsms.append(m)
    sched = [("tx", 5, "cmd", {"target": -1, "cmd": "send_data"})] + \
            [("rx", cfg["rx_ms"], "listen") for _ in range(cfg["slots"])]
    beh = bsengine.BSBehavior(env=env, base_station=bs, schedule=sched,
                              backscatter_modules=bsms, loop=True,
                              enable_sic=cfg["sic"])
    beh.nodes_registered = [n.id for n in nodes]
    for m in bsms:
        m.bs_processes = [beh]
    env.run(until=SIM_MS)
    sent = sum(m.packets_sent for m in bsms)
    decoded = len({(p.node_id, p.start_ms) for p in beh.rx_packets
                   if not p.collided})
    return {"sent": sent, "decoded": decoded,
            "collided": sum(1 for p in beh.rx_packets if p.collided)}


BASE = dict(C=300e-6, rx_ms=10, slots=2, beamwidth=65, sector_sens=-100)
SICDEMO = dict(C=200e-6, rx_ms=15, slots=3, beamwidth=30, sector_sens=-80)


def job(a):
    name, cfg, seed = a
    return name, seed, run(cfg, seed)


if __name__ == "__main__":
    seeds = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    out = sys.argv[2] if len(sys.argv) > 2 else "results/sic_ablation.json"
    cfgs = {}
    for sic in (False, True):
        tag = "sic" if sic else "nosic"
        cfgs[f"shipped-broadcast/{tag}"] = {**BASE, "sic": sic}
        cfgs[f"shipped-sic-demo/{tag}"] = {**SICDEMO, "sic": sic}
    # one-factor-at-a-time from the broadcast config (SIC off)
    for k, v in (("C", 200e-6), ("rx_ms", 15), ("slots", 3),
                 ("beamwidth", 30), ("sector_sens", -80)):
        cfgs[f"one-factor {k}={v}/nosic"] = {**BASE, k: v, "sic": False}
    jobs = [(n, c, s) for n, c in cfgs.items() for s in range(seeds)]
    with Pool(4) as p:
        res = p.map(job, jobs, chunksize=1)
    agg = {}
    for n, s, r in res:
        agg.setdefault(n, []).append(r)
    json.dump({"cfgs": cfgs, "runs": agg}, open(out, "w"))
    for n, rs in agg.items():
        ders = [1 - r["decoded"] / r["sent"] for r in rs]
        thr = [r["decoded"] / (SIM_MS / 1000) for r in rs]
        m = sum(ders) / len(ders)
        sd = (sum((x - m) ** 2 for x in ders) / len(ders)) ** 0.5
        print(f"{n:42s} DER {100*m:5.2f}% (sd {100*sd:4.2f})  "
              f"thr {sum(thr)/len(thr):5.2f} pkt/s  sent {sum(r['sent'] for r in rs)/len(rs):6.1f}")
