"""
Case study for the textbook chapter "Extending Amber": does an *energy-aware*
unicast polling policy beat plain round-robin polling?

Both policies are written as BS policies (generators of frames) on top of the
unmodified engine; nodes use the default BackscatterModule (registered state).

  round_robin : poll node 0,1,...,N-1,0,1,... one (tx 5 ms + rx 10 ms) frame each.
  skip_silent : same, but a node that fails to answer is skipped for the next
                `PENALTY` polls (it is probably recharging).

Metrics over SIM_MS: decoded packets, polls issued, decoded/poll, per-node
min/max decoded (fairness).
"""
import json, math, os, random, sys
from multiprocessing import Pool
import simpy
from amber import (backscatter, bsengine, capacitor, controller, energy,
                   propagation, radiodevices)

HERE = os.path.dirname(os.path.abspath(__file__))
TRACE = os.path.join(HERE, "..", "..", "demo_experiments", "threehundred_seconds_stable.xlsx")
N = 12
SIM_MS = 20_001
PENALTY = 8          # polls to skip after a silent poll


def run(policy_name, seed, penalty=PENALTY):
    random.seed(seed)
    env = simpy.Environment()
    src = energy.EnvEnergySource(env=env, file_path=TRACE, column="V_IM", resistance=5e3)
    nodes = []
    for i in range(N):
        r = random.uniform(10.0, 40.0)            # stay beyond the 10 m path-loss artefact
        a = random.uniform(0, 2 * math.pi)
        nodes.append(radiodevices.Node(id=i, x=r * math.cos(a), y=r * math.sin(a),
                                       efficiency=0.7, sensitivity_dbm=-100))
    bs = radiodevices.BaseStation(id=0, x=0, y=0, sectors=[radiodevices.Sector(
        azimuth_deg=a, beamwidth_deg=65, power=46, antenna_gain_dbi=15,
        sensitivity_dbm=-100, height=25) for a in (0, 120, 240)])
    cov = propagation.CoverageMap(base_stations=[bs], nodes=nodes, freq_hz=924e6,
                                  pathloss_model="macro", node_energy_mode="hybrid",
                                  node_ext_power_fn=lambda n: src.ext_power, combine_mode="max")
    dl = cov.compute_bs_to_point(nodes); cov.calculate_node_power(nodes, dl)
    ul = cov.compute_point_to_bs(nodes)
    ctl = controller.ControllerParams(controller.CurrentsA(1.4e-4, 0.512e-3, 1.28e-3, 5e-3),
                                      controller.DurationsMs(5, 2, 5, 15),
                                      controller.VoltageThresholdsV(1.7, 1.3))
    mods = []
    for nd in nodes:
        cap = capacitor.Capacitor(env=env, id=nd.id, keep_logs=False)
        m = backscatter.BackscatterModule(env=env, node=nd, bs_processes=[], uplink_results=ul,
                                          downlink_results=dl, keep_logs=False)
        m.state = "registered"
        controller.Controller(env=env, capacitor_ctrl=cap, node=nd, backscatter=m, params=ctl,
                              coverage_map=cov, downlink_results=dl, keep_logs=False)
        mods.append(m)

    polls = {"n": 0}

    def policy(b):
        skip = {i: 0 for i in range(N)}
        i = 0
        while True:
            if policy_name == "skip_silent" and skip[i] > 0:
                skip[i] -= 1
                i = (i + 1) % N
                continue
            yield [("tx", 5, "poll", {"cmd": "send_data", "target": i}),
                   ("rx", 10, "slot", {"expect": i})]
            polls["n"] += 1
            if policy_name == "skip_silent" and i not in b.decoded_this_frame:
                skip[i] = penalty
            i = (i + 1) % N

    beh = bsengine.BSBehavior(env=env, base_station=bs, policy=policy, backscatter_modules=mods,
                              keep_logs=False, enable_sic=True)
    for m in mods:
        m.bs_processes = [beh]
    env.run(until=SIM_MS)
    per = [beh.rx_count_by_node[i] for i in range(N)]
    dec = sum(per)
    return {"decoded": dec, "polls": polls["n"], "per_node": per, "collided": beh.total_collided}


def job(a):
    return a[0], a[2], run(a[0], a[2], a[1])


if __name__ == "__main__":
    seeds = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    out = sys.argv[2] if len(sys.argv) > 2 else "results/polling.json"
    variants = [("round_robin", PENALTY)] + [("skip_silent", p) for p in (2, 4, 8, 16)]
    jobs = [(n, p, s) for n, p in variants for s in range(seeds)]
    with Pool(4) as pool:
        res = pool.map(job, jobs)
    agg = {}
    for n, s, r in res:
        agg.setdefault(n, []).append(r)
    # the loop above lost the penalty in the key; rebuild keys
    agg = {}
    for (n, p, s), (_, _, r) in zip(jobs, res):
        agg.setdefault(f"{n}:{p}" if n == "skip_silent" else n, []).append(r)
    json.dump(agg, open(out, "w"))
    for k, rs in agg.items():
        d = [r["decoded"] for r in rs]; p = [r["polls"] for r in rs]
        eff = [a / b for a, b in zip(d, p)]
        mn = [min(r["per_node"]) for r in rs]; mx = [max(r["per_node"]) for r in rs]
        f = lambda x: sum(x) / len(x)
        print(f"{k:16s} decoded {f(d):7.1f}  polls {f(p):7.1f}  decoded/poll {f(eff):5.3f}  "
              f"thr {f(d)/(SIM_MS/1000):5.2f} pkt/s  per-node min/max {f(mn):5.1f}/{f(mx):5.1f}")
