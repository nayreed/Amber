"""Does the active-mode energy credit (Vs*I*dt vs. delivered V*I*dt) matter for the demos?
Runs the broadcast scenario (SIC on, to isolate energy effects) with the stock capacitor
and with a patched one that credits V*I*dt.  Prints mean packets sent per node."""
import math, sys, random
from multiprocessing import Pool
sys.path.insert(0, ".")
import sic_ablation as sa
from amber import capacitor as C

def patched(self, load_current):
    p = self.params
    charge_current = max((self.voltage_source - self.voltage) / p.R_series, 0.0)
    energy_harvested = self.voltage * charge_current * p.dt          # delivered, not source
    energy_consumed = self.voltage * load_current * p.dt
    self.energy = max(self.energy + energy_harvested - energy_consumed, 0.0)
    self.voltage = math.sqrt(2.0 * self.energy / p.C)
    self.voltage *= math.exp(-p.dt / (p.R_leakage * p.C))
    self.voltage = min(max(self.voltage, 0.0), self.voltage_max)
    self.energy = 0.5 * p.C * self.voltage * self.voltage
    self._tick_common()
    return self.voltage

def job(a):
    mode, seed = a
    if mode == "delivered":
        C.Capacitor.charge_and_discharge = patched
    cfg = {**sa.BASE, "sic": True}
    return mode, seed, sa.run(cfg, seed)

if __name__ == "__main__":
    jobs = [(m, s) for m in ("stock", "delivered") for s in range(10)]
    with Pool(4) as p:
        res = p.map(job, jobs)
    for m in ("stock", "delivered"):
        r = [x[2]["sent"] for x in res if x[0] == m]
        print(m, "mean packets sent (3 nodes, 20 s): %.1f  (sd %.1f)" % (sum(r)/len(r),
              (sum((a - sum(r)/len(r))**2 for a in r)/len(r))**0.5))
