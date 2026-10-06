"""Wall-clock cost of Amber as a function of node count (idle base station,
all nodes charging/duty-cycling).  Reports microseconds per node-millisecond."""
import math, random, sys, time
import simpy
from amber import (backscatter, bsengine, capacitor, controller, energy,
                   propagation, radiodevices)
TRACE = "../../demo_experiments/threehundred_seconds_stable.xlsx"

def build_and_run(n, sim_ms):
    random.seed(1)
    env = simpy.Environment()
    src = energy.EnvEnergySource(env=env, file_path=TRACE, column="V_IM", resistance=5e3)
    nodes = [radiodevices.Node(id=i, x=100*math.sqrt(random.random())*math.cos(a),
                               y=100*math.sqrt(random.random())*math.sin(a),
                               efficiency=0.7, antenna_gain_dbi=1.0)
             for i, a in enumerate(random.uniform(0, 6.28) for _ in range(n))]
    bs = radiodevices.BaseStation(id=0, x=0, y=0, sectors=[radiodevices.Sector(
        azimuth_deg=a, beamwidth_deg=65, power=30, antenna_gain_dbi=17, height=25) for a in (0,120,240)])
    cov = propagation.CoverageMap(base_stations=[bs], nodes=nodes, freq_hz=924e6,
        pathloss_model="macro", node_energy_mode="hybrid",
        node_ext_power_fn=lambda nd: src.ext_power)
    t0 = time.perf_counter()
    dl = cov.compute_bs_to_point(nodes); cov.calculate_node_power(nodes, dl); ul = cov.compute_point_to_bs(nodes)
    t_pre = time.perf_counter() - t0
    ctl = controller.ControllerParams(controller.CurrentsA(1.4e-4,0.512e-3,1.28e-3,5e-3),
        controller.DurationsMs(5,2,5,15), controller.VoltageThresholdsV(1.7,1.3))
    for nd in nodes:
        cap = capacitor.Capacitor(env=env, id=nd.id, keep_logs=False)
        m = backscatter.BackscatterModule(env=env, node=nd, bs_processes=[], uplink_results=ul,
                                          downlink_results=dl, keep_logs=False)
        controller.Controller(env=env, capacitor_ctrl=cap, node=nd, backscatter=m,
                              params=ctl, coverage_map=cov, downlink_results=dl, keep_logs=False)
    t1 = time.perf_counter(); env.run(until=sim_ms); t_run = time.perf_counter() - t1
    return t_pre, t_run

if __name__ == "__main__":
    print("nodes  precompute(s)  run(s) for 5 s sim   us per node-ms")
    for n in (10, 30, 100, 300):
        tp, tr = build_and_run(n, 5000)
        print(f"{n:5d}  {tp:12.4f}  {tr:10.2f}   {1e6*tr/(n*5000):10.1f}")
