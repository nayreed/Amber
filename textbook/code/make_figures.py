"""
Generates every data-driven figure used in the Amber textbook.

Usage:  python make_figures.py [name ...]      (no name = all figures)

All simulator-derived figures call the real `amber` package; nothing is drawn
from hand-typed numbers.  Output: ../figures/*.pdf (+ a PNG preview next to it
when the environment variable AMBER_PREVIEW_DIR is set).
"""
import importlib.util
import json
import math
import os
import sys
import tempfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import simpy

from amber import capacitor, controller, propagation as P, radiodevices as R

HERE = os.path.dirname(os.path.abspath(__file__))
FIG = os.path.join(HERE, "..", "figures")
DEMOS = os.path.join(HERE, "..", "..", "demo_experiments")
PREVIEW = os.environ.get("AMBER_PREVIEW_DIR")
os.makedirs(FIG, exist_ok=True)

# ----------------------------------------------------------------- style ----
BLUE, ORANGE, GREEN, RED, PURPLE, GREY, TEAL = (
    "#1f5fa8", "#e07b00", "#2a8f4a", "#c9302c", "#7b4fa6", "#6b6b6b", "#16808a")
plt.rcParams.update({
    "font.family": "serif", "font.serif": ["STIXGeneral", "DejaVu Serif"],
    "mathtext.fontset": "stix", "font.size": 9, "axes.labelsize": 9,
    "axes.titlesize": 9, "legend.fontsize": 8, "xtick.labelsize": 8,
    "ytick.labelsize": 8, "axes.grid": True, "grid.alpha": 0.25,
    "grid.linewidth": 0.5, "axes.spines.top": False,
    "axes.spines.right": False, "figure.dpi": 150, "savefig.bbox": "tight",
    "savefig.pad_inches": 0.04, "axes.prop_cycle":
        matplotlib.cycler(color=[BLUE, ORANGE, GREEN, RED, PURPLE, TEAL]),
})

FIGURES = {}


def figure(fn):
    FIGURES[fn.__name__] = fn
    return fn


def save(fig, name):
    fig.savefig(os.path.join(FIG, name + ".pdf"))
    if PREVIEW:
        fig.savefig(os.path.join(PREVIEW, name + ".png"), dpi=110)
    plt.close(fig)
    print("wrote", name)


# ------------------------------------------------------------ propagation ---
@figure
def fig_pathloss():
    d = np.logspace(0.7, 3.0, 400)                       # 5 m .. 1 km
    f = 924e6
    fs = 10 * np.log10(1 / P.fspl_lin(d, f))
    los = P.urban_macro_loss_db_38_901(d, f, 25.0, 1.5, True)
    nlos = P.urban_macro_loss_db_38_901(d, f, 25.0, 1.5, False)
    dbp = 4 * (25 - 1) * (1.5 - 1) * f / P.C
    fig, ax = plt.subplots(figsize=(5.6, 3.0))
    ax.plot(d, fs, color=GREY, ls="--", label="FSPL (Friis), $n=2$")
    ax.plot(d, los, color=BLUE, label="3GPP UMa LOS")
    ax.plot(d, nlos, color=ORANGE, label="3GPP UMa NLOS")
    ax.axvline(dbp, color=BLUE, lw=0.8, ls=":")
    ax.text(dbp * 1.06, 132, r"$d'_{BP}\approx %.0f$ m" % dbp, color=BLUE,
            fontsize=8, va="top")
    ax.axvspan(5, 10, color=RED, alpha=0.08, lw=0)
    ax.text(7.07, 112, "$d_{2D}<10$ m:\nbranch\nfall-through", color=RED,
            fontsize=7, ha="center", va="top")
    ax.set_xscale("log")
    ax.set_xlabel("2-D distance $d_{2D}$ (m)")
    ax.set_ylabel("Path loss (dB)")
    ax.set_xlim(5, 1000)
    ax.set_ylim(40, 135)
    ax.legend(loc="upper left", bbox_to_anchor=(0.2, 1.0), frameon=False)
    save(fig, "fig_pathloss")


@figure
def fig_antenna():
    ang = np.linspace(-180, 180, 721)
    fig, ax = plt.subplots(figsize=(5.6, 3.0))
    for bw, c in ((30, GREEN), (65, BLUE), (120, ORANGE)):
        a = P.SectorAntenna3GPP(0.0, bw, 15.0)
        g = 10 * np.log10([a.gain_linear(x) for x in ang])
        ax.plot(ang, g, color=c, label=r"$\varphi_{3dB}=%d^\circ$" % bw)
        ax.plot([-bw / 2, bw / 2], [12, 12], "o", color=c, ms=3)
    ax.axhline(15 - 30, color=GREY, ls=":", lw=0.8)
    ax.text(178, -13.7, r"floor $G_{max}-A_m$", color=GREY, fontsize=8,
            va="bottom", ha="right")
    ax.set_xlabel(r"Angular error from boresight $\Delta\varphi$ (deg)")
    ax.set_ylabel("Gain (dBi)")
    ax.set_xlim(-180, 180)
    ax.set_xticks(range(-180, 181, 60))
    ax.legend(frameon=False, loc="upper right")
    save(fig, "fig_antenna")


def _demo_budget():
    mk = lambda i, y: R.Node(id=i, x=0, y=y, height=1.5,
                             sensitivity_dbm=-100, efficiency=0.7)
    bs = R.BaseStation(id=0, x=0, y=0, sectors=[
        R.Sector(azimuth_deg=0, beamwidth_deg=65, power=46,
                 antenna_gain_dbi=15, sensitivity_dbm=-100, height=25)])
    ys = np.array([10, 12, 15, 20, 30, 40, 50, 65, 80, 100, 130, 170, 220, 300, 400, 500.0])
    nodes = [mk(i, y) for i, y in enumerate(ys)]
    cov = P.CoverageMap(base_stations=[bs], nodes=nodes, freq_hz=924e6,
                        pathloss_model="macro", los=True,
                        node_energy_mode="wpt")
    dl = cov.compute_bs_to_point(nodes)
    cov.calculate_node_power(nodes, dl)
    ul = cov.compute_point_to_bs(nodes)
    dlp = np.array([dl["best_pw_dbm"][n.id] for n in nodes])
    ulp = np.array([ul["per_sector_powers"]["BS0_S0"][n.id] for n in nodes])
    return ys, dlp, ulp


@figure
def fig_budget():
    ys, dlp, ulp = _demo_budget()
    fig, ax = plt.subplots(figsize=(5.6, 3.1))
    ax.plot(ys, dlp, "-o", ms=3, color=BLUE,
            label="Downlink: power arriving at the node")
    ax.plot(ys, ulp, "-s", ms=3, color=ORANGE,
            label=r"Uplink: backscatter power at the BS ($\eta=0.7$)")
    ax.axhline(-100, color=RED, ls="--", lw=0.9)
    ax.text(10.5, -98, "BS sector sensitivity $-100$ dBm", color=RED,
            fontsize=8, va="bottom")
    for y, name in ((20, "N0"), (30, "N1"), (100, "N2")):
        ax.axvline(y, color=GREY, lw=0.6, ls=":")
        ax.text(y, 8, name, ha="center", fontsize=8, color=GREY)
    ax.set_xscale("log")
    ax.set_xlabel("Distance of node from BS along boresight (m)")
    ax.set_ylabel("Power (dBm)")
    ax.set_ylim(-125, 12)
    ax.set_xlim(10, 520)
    ax.legend(frameon=False, loc="lower left")
    save(fig, "fig_budget")


# --------------------------------------------------------------- capacitor ---
def _cap_run(Vs, steps, V0=0.0, vmax=2.0, mode="charge", params=None):
    env = simpy.Environment()
    cap = capacitor.Capacitor(env=env, id=0, initial_voltage=V0,
                              voltage_max=vmax, keep_logs=False,
                              params=params or capacitor.CapacitorParams())
    cap.voltage_source = Vs
    out = np.empty(steps)
    for k in range(steps):
        if mode == "charge":
            cap.capacitor_charging()
        else:
            cap.charge_and_discharge(0.0)
        out[k] = cap.voltage
    return out


@figure
def fig_cap_charge():
    p = capacitor.CapacitorParams()
    t = np.arange(1, 6001) * 1e-3
    fig, ax = plt.subplots(figsize=(5.6, 3.1))
    for Vs, c in ((2.486, BLUE), (1.971, ORANGE), (0.662, GREEN)):
        v = _cap_run(Vs, 6000)
        ax.plot(t, v, color=c, label=r"$V_s=%.2f$ V (Amber)" % Vs)
        vinf = Vs * p.R_leakage / (p.R_leakage + p.R_series)
        tau = 1 / (1 / (p.R_series * p.C) + 1 / (p.R_leakage * p.C))
        ana = vinf * (1 - np.exp(-t / tau))
        ax.plot(t[::300], np.minimum(ana, 2.0)[::300], "k.", ms=3)
    ax.plot([], [], "k.", ms=3, label="exact RC solution")
    ax.axhline(1.7, color=GREY, ls="--", lw=0.8)
    ax.axhline(1.3, color=GREY, ls="--", lw=0.8)
    ax.axhline(2.0, color=RED, ls=":", lw=0.8)
    ax.text(5.98, 1.72, r"$V_{high}$", ha="right", fontsize=8, color=GREY)
    ax.text(5.98, 1.32, r"$V_{low}$", ha="right", fontsize=8, color=GREY)
    ax.text(5.98, 2.02, r"$V_{max}$ clamp", ha="right", fontsize=8, color=RED)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Capacitor voltage (V)")
    ax.set_ylim(0, 2.25)
    ax.legend(frameon=False, loc="center left", bbox_to_anchor=(1.01, 0.5))
    save(fig, "fig_cap_charge")


@figure
def fig_energy_accounting():
    t = np.arange(1, 2001) * 1e-3
    a = _cap_run(2.5, 2000, V0=1.0, vmax=10, mode="charge")
    b = _cap_run(2.5, 2000, V0=1.0, vmax=10, mode="cd")
    p = capacitor.CapacitorParams()
    vinf = 2.5 * p.R_leakage / (p.R_leakage + p.R_series)
    tau = 1 / (1 / (p.R_series * p.C) + 1 / (p.R_leakage * p.C))
    ana = vinf + (1.0 - vinf) * np.exp(-t / tau)
    fig, ax = plt.subplots(figsize=(5.6, 3.0))
    ax.plot(t, a, color=BLUE, lw=2.4,
            label=r"charge-only step ($\Delta V=\frac{dt}{R_sC}(V_s-V)$)")
    ax.plot(t, b, color=RED, lw=1.6,
            label=r"charge-and-discharge step, load $=0$ ($\Delta E=V_s I\,dt$)")
    ax.plot(t, ana, color="k", ls=(0, (2, 3)), lw=1.1, zorder=5,
            label="exact RC solution (coincides with blue)")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Capacitor voltage (V)")
    ax.legend(frameon=False, loc="lower right")
    save(fig, "fig_energy_accounting")


@figure
def fig_power_transfer():
    """Power delivered into the capacitor vs V/Vsrc, and what the active-mode
    step credits."""
    x = np.linspace(0, 1, 401)
    fig, ax = plt.subplots(figsize=(5.2, 2.9))
    ax.plot(x, x * (1 - x), color=BLUE, lw=1.8,
            label=r"physically delivered into $C$:  $x(1-x)$")
    ax.plot(x, 1 - x, color=RED, lw=1.8, ls="--",
            label=r"credited by active-mode step:  $(1-x)$")
    ax.plot([0.5], [0.25], "o", color=BLUE, ms=4)
    ax.annotate(r"max $=\frac{1}{4}$ at $V=V_{src}/2$", (0.5, 0.25),
                (0.06, 0.42), fontsize=8, arrowprops=dict(arrowstyle="-",
                                                            lw=0.6))
    for xv, lab in ((1.5 / 2.486, r"$V=1.5$ V,  node 0"),):
        ax.axvline(xv, color=GREY, lw=0.7, ls=":")
        ax.text(xv + 0.01, 0.92, lab, fontsize=7.5, color=GREY)
    ax.set_xlabel(r"Capacitor voltage relative to source, $x=V/V_{src}$")
    ax.set_ylabel(r"Power / $P_h$,  $P_h=V_{src}^2/R_s$")
    ax.set_ylim(0, 1.02)
    ax.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.5, 1.28),
              fontsize=7.8)
    save(fig, "fig_power_transfer")


# ------------------------------------------------- full-demo trace figures ---
def _load_demo(name, subdir):
    path = os.path.join(DEMOS, subdir, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.OUTPUT_DIR = tempfile.mkdtemp()          # never touch committed CSVs
    return mod


_DEMO_CACHE = {}


def _broadcast_trace():
    """Run the shipped broadcast demo with a per-ms monitor on every node."""
    if "bc" in _DEMO_CACHE:
        return _DEMO_CACHE["bc"]
    import random
    random.seed(11)
    mod = _load_demo("broadcast_demo", "broadcast")
    mons = {}
    orig = controller.Controller.__init__

    def patched(self, *a, **k):
        orig(self, *a, **k)
        rec = mons.setdefault(self.id, [])

        def monitor(env=self.env, ctl=self, rec=rec):
            while True:
                yield env.timeout(1)
                rec.append((env.now, ctl.state_name, ctl.is_active,
                            ctl.capacitor_ctrl.voltage,
                            ctl.node.harvesting_power_dbm))
        self.env.process(monitor())

    controller.Controller.__init__ = patched
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            sim = mod.run_simulation()
    finally:
        controller.Controller.__init__ = orig
    _DEMO_CACHE["bc"] = (sim, mons, mod)
    return _DEMO_CACHE["bc"]


@figure
def fig_sawtooth():
    sim, mons, mod = _broadcast_trace()
    fig, axes = plt.subplots(3, 1, figsize=(6.2, 5.0), sharex=True)
    names = ["Node 0 (20 m, WPT)", "Node 1 (30 m, WPT)",
             "Node 2 (100 m, hybrid)"]
    for ax, cap, nm, c in zip(axes, sim["capacitors"], names,
                              (BLUE, ORANGE, GREEN)):
        t, v = zip(*cap.voltage_history)
        ax.plot(t, v, color=c, lw=0.8)
        ax.axhline(1.7, color=GREY, ls="--", lw=0.7)
        ax.axhline(1.3, color=GREY, ls="--", lw=0.7)
        ax.set_ylabel("$V_C$ (V)")
        ax.set_ylim(0, 2.1)
        ax.set_title(nm, fontsize=8.5, loc="left", pad=2)
    axes[-1].set_xlabel("Time (s)")
    axes[-1].set_xlim(0, 20)
    save(fig, "fig_sawtooth")


STATE_COL = {"charging (off)": "#d9d9d9", "listening": BLUE,
             "sensing": GREEN, "processing": PURPLE, "wait slot": "#f2c14e",
             "transmitting": RED}


@figure
def fig_fsm_zoom():
    """One command/response exchange of node 0: voltage, node FSM, BS slots."""
    sim, mons, mod = _broadcast_trace()
    rec = mons[0]
    i0 = next(i for i, r in enumerate(rec) if r[2])        # first activation
    txs = [r.end_ms for r in sim["backscatter_modules"][0].tx_records
           if r.end_ms > rec[i0][0]]
    t_tx = txs[1]                                          # 2nd packet
    t0, t1 = t_tx - 45, t_tx + 75
    win = [r for r in rec if t0 <= r[0] <= t1]
    ts = np.array([r[0] for r in win])
    vs = np.array([r[3] for r in win])

    fig, (a1, a2, a3) = plt.subplots(
        3, 1, figsize=(6.6, 4.3), sharex=True,
        gridspec_kw={"height_ratios": [2.0, 0.8, 0.8], "hspace": 0.25})
    a1.plot(ts, vs, color=BLUE, lw=1.2)
    a1.axhline(1.3, color=GREY, ls="--", lw=0.7)
    a1.set_ylabel("$V_C$ (V)")
    lo, hi = vs.min(), vs.max()
    a1.set_ylim(lo - 0.04, hi + 0.04)

    def label(r):
        return "charging (off)" if not r[2] else r[1].replace("_", " ")
    prev, start = label(win[0]), win[0][0]
    segs = []
    for r in win[1:]:
        if label(r) != prev:
            segs.append((start, r[0], prev))
            prev, start = label(r), r[0]
    segs.append((start, win[-1][0], prev))
    for s_, e_, st in segs:
        a2.barh(0, e_ - s_, left=s_, height=0.9, color=STATE_COL[st],
                edgecolor="white", linewidth=0.3)
    a2.set_yticks([0])
    a2.set_yticklabels(["node FSM"], fontsize=8)
    a2.set_ylim(-0.6, 0.6)
    a2.grid(False)

    # BS schedule: frame = tx(5) + rx(10) + rx(10), period 25 ms from t = 0
    k0, k1 = int(t0 // 25), int(t1 // 25) + 1
    for k in range(k0, k1 + 1):
        base = 25 * k
        a3.barh(0, 5, left=base, height=0.9, color=ORANGE, edgecolor="white",
                linewidth=0.3)
        a3.barh(0, 10, left=base + 5, height=0.9, color="#8fc7cc",
                edgecolor="white", linewidth=0.3)
        a3.barh(0, 10, left=base + 15, height=0.9, color=TEAL,
                edgecolor="white", linewidth=0.3)
        for cx, lab, col in ((base + 2.5, "TX", "k"), (base + 10, "RX1", "k"),
                             (base + 20, "RX2", "white")):
            if t0 + 4 <= cx <= t1 - 4:
                a3.text(cx, 0, lab, ha="center", va="center", fontsize=6.5,
                        color=col)
    for tt in txs:
        if t0 <= tt <= t1:
            a3.plot([tt], [0.62], "v", color="k", ms=5, clip_on=False)
            a2.plot([tt], [0.78], "v", color="k", ms=5, clip_on=False)
    a3.set_yticks([0])
    a3.set_yticklabels(["BS slots"], fontsize=8)
    a3.set_ylim(-0.6, 0.6)
    a3.set_xlim(t0, t1)
    a3.grid(False)
    a3.set_xlabel("Simulation time (ms)   (black triangles: node backscatters)")
    handles = [matplotlib.patches.Patch(color=c, label=n)
               for n, c in STATE_COL.items()]
    a3.legend(handles=handles, ncol=6, frameon=False, loc="upper center",
              bbox_to_anchor=(0.5, -0.8), fontsize=7, handlelength=1.2,
              columnspacing=0.9)
    save(fig, "fig_fsm_zoom")


# --------------------------------------------------------------------MAC ----
@figure
def fig_aloha():
    g = np.linspace(0, 4, 400)
    fig, (a, b) = plt.subplots(1, 2, figsize=(6.6, 2.8))
    a.plot(g, g * np.exp(-2 * g), color=ORANGE, label="pure ALOHA")
    a.plot(g, g * np.exp(-g), color=BLUE, label="slotted ALOHA")
    a.plot([0.5], [0.5 * math.exp(-1)], "o", color=ORANGE, ms=4)
    a.plot([1], [math.exp(-1)], "o", color=BLUE, ms=4)
    a.text(0.55, 0.22, r"$\frac{1}{2e}\approx0.184$", fontsize=8,
           color=ORANGE)
    a.text(1.05, 0.40, r"$\frac{1}{e}\approx0.368$", fontsize=8, color=BLUE)
    a.set_xlabel("Offered load $g$ (attempts per slot)")
    a.set_ylabel("Throughput (successes per slot)")
    a.legend(frameon=False)
    for S, c in ((8, GREEN), (16, BLUE), (32, ORANGE)):
        n = np.arange(1, 81)
        b.plot(n, (1 - 1 / S) ** (n - 1), color=c, label=f"$S={S}$ slots")
    b.set_xlabel("Contending nodes $n$")
    b.set_ylabel(r"$P_{succ}=(1-1/S)^{n-1}$")
    b.legend(frameon=False)
    fig.tight_layout()
    save(fig, "fig_aloha")


@figure
def fig_frame_efficiency():
    r = np.linspace(0.05, 4, 400)             # r = n / S
    fig, ax = plt.subplots(figsize=(4.2, 2.8))
    for S, c in ((8, GREEN), (32, BLUE), (1000, ORANGE)):
        n = r * S
        eff = n * (1 - 1 / S) ** (n - 1) / S
        ax.plot(r, eff, color=c, label=f"$S={S}$" if S < 1000 else
                r"$S\to\infty$: $re^{-r}$")
    ax.axvline(1, color=GREY, ls=":", lw=0.8)
    ax.set_xlabel("Load ratio $n/S$")
    ax.set_ylabel("Slot efficiency (successes per slot)")
    ax.legend(frameon=False)
    save(fig, "fig_frame_efficiency")


# ------------------------------------------------------------- SINR / SIC ---
@figure
def fig_sinr_vs_gap():
    """Two colliding packets: SINR of the stronger one vs power gap."""
    gap = np.linspace(0, 20, 200)
    fig, ax = plt.subplots(figsize=(4.8, 2.8))
    noise_dbm = -88.0
    for p1, c in ((-60, BLUE), (-80, ORANGE)):
        p2 = p1 - gap
        s = 10 ** (p1 / 10) / (10 ** (p2 / 10) + 10 ** (noise_dbm / 10))
        ax.plot(gap, 10 * np.log10(s), color=c,
                label=f"stronger packet at {p1} dBm")
    ax.axhline(3, color=RED, ls="--", lw=0.9)
    ax.text(19.8, 3.4, r"$\gamma_{req}=3$ dB (BS default)", color=RED,
            ha="right", fontsize=8)
    ax.set_xlabel("Power gap between the two packets (dB)")
    ax.set_ylabel("SINR of stronger packet (dB)")
    ax.legend(frameon=False, loc="upper left")
    save(fig, "fig_sinr_vs_gap")


# ----------------------------------------------- coverage map (paper Fig 1) ---
@figure
def fig_coverage():
    import random
    rng = random.Random(5)
    bss = [R.BaseStation(id=i, x=x, y=y, sectors=[
        R.Sector(azimuth_deg=a, beamwidth_deg=65, power=30,
                 antenna_gain_dbi=17, sensitivity_dbm=-100, height=25)
        for a in (0, 120, 240)], site_radius=5)
        for i, (x, y) in enumerate([(150, 330), (310, 340), (230, 130)])]
    nodes = [R.Node(id=i, x=rng.uniform(100, 450), y=rng.uniform(100, 420),
                    antenna_gain_dbi=1.0, radius=4, label=False)
             for i in range(15)]
    cov = P.CoverageMap(base_stations=bss, nodes=nodes, freq_hz=924e6,
                        pathloss_model="macro", los=True)
    cov.compute_coverage_map(80, 480, 80, 440, step_m=2.0)
    fig, ax = plt.subplots(figsize=(5.4, 4.4))
    im, cb = cov.plot_rssi(ax=ax, vmin=-60, vmax=-20, fontsize=8,
                           fontsize_radio=7)
    save(fig, "fig_coverage")


# ------------------------------------------------------ custom protocol -----
@figure
def fig_adaptation():
    import pandas as pd
    df = pd.read_csv(os.path.join(DEMOS, "custom_protocol", "output",
                                  "adaptation.csv"))
    fig, (a, b) = plt.subplots(2, 1, figsize=(6.0, 4.0), sharex=True,
                               gridspec_kw={"height_ratios": [1.1, 1]})
    a.step(df.time_s, df.n_slots, where="post", color=BLUE, lw=1.0)
    a.set_ylabel("frame size (slots)")
    a.set_ylim(0, df.n_slots.max() * 1.1)
    nb = 30
    edges = np.linspace(df.time_s.min(), df.time_s.max(), nb + 1)
    idx = np.digitize(df.time_s, edges[1:-1])
    dec = np.bincount(idx, weights=df.heard, minlength=nb)
    col = np.bincount(idx, weights=df.collided, minlength=nb)
    ctr = 0.5 * (edges[:-1] + edges[1:])
    w = (edges[1] - edges[0]) * 0.85
    b.bar(ctr, dec, width=w, color=GREEN, label="decoded")
    b.bar(ctr, col, width=w, bottom=dec, color=RED, label="collided")
    b.set_xlabel("Time (s)")
    b.set_ylabel(f"packets per {edges[1]-edges[0]:.1f} s")
    b.legend(frameon=False, loc="upper right")
    fig.tight_layout()
    save(fig, "fig_adaptation")


# ------------------------------------------------- registration experiment ---
@figure
def fig_registration():
    path = os.path.join(HERE, "results", "registration_results.json")
    with open(path) as f:
        J = json.load(f)
    S, K, ROUNDS = J["slots"], J["shifts"], J["rounds"]
    res = J["results"]
    dens = sorted({int(k.split(":")[1]) for k in res})

    def stats(mode, r):
        mean, lo, hi = [], [], []
        for n in dens:
            v = np.array([run[r] for run in res[f"{mode}:{n}"]]) * 100
            mean.append(v.mean())
            lo.append(np.percentile(v, 10))
            hi.append(np.percentile(v, 90))
        return np.array(mean), np.array(lo), np.array(hi)

    fig, (a, b) = plt.subplots(1, 2, figsize=(7.0, 3.0), sharey=True)
    for r, c in ((0, RED), (1, ORANGE), (3, BLUE)):
        m, lo, hi = stats("nosic", r)
        a.plot(dens, m, "-o", ms=3, color=c,
               label=f"after round {r+1}")
        a.fill_between(dens, lo, hi, color=c, alpha=0.12, lw=0)
    a.set_title("No SIC (capture only)")
    b.set_title("With SIC")
    for r, c in ((0, RED), (1, ORANGE), (3, BLUE)):
        m, lo, hi = stats("sic", r)
        b.plot(dens, m, "-o", ms=3, color=c, label=f"after round {r+1}")
        b.fill_between(dens, lo, hi, color=c, alpha=0.12, lw=0)
    # analytic mean-field model: collision-free-bin success, 2S advertised
    # slots of which only S are in the current frame
    n = np.array(dens, float)
    unreg = n.copy()
    reg = 0 * n
    for r in range(ROUNDS):
        # each unregistered node participates w.p. 1/2 (real half of the list),
        # then picks a (slot, shift) bin among S*K bins uniformly
        active = unreg / 2.0
        p_alone = (1 - 1.0 / (S * K)) ** np.maximum(active - 1, 0)
        succ = active * p_alone
        unreg = unreg - succ
        reg = 100 * (n - unreg) / n
        if r in (0, 1, 3):
            a.plot(n, reg, "--", color={0: RED, 1: ORANGE, 3: BLUE}[r], lw=1)
    a.plot([], [], "k--", lw=1, label="analytic model")
    a.set_xlabel("Number of nodes")
    b.set_xlabel("Number of nodes")
    a.set_ylabel("Registered nodes (%)")
    a.set_ylim(0, 105)
    h, l = a.get_legend_handles_labels()
    fig.legend(h, l, frameon=False, fontsize=8, loc="lower center", ncol=4,
               bbox_to_anchor=(0.5, -0.06))
    fig.tight_layout()
    save(fig, "fig_registration")


if __name__ == "__main__":
    names = sys.argv[1:] or list(FIGURES)
    for nme in names:
        FIGURES[nme]()
