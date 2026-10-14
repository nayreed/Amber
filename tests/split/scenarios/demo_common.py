# tests/split/scenarios/demo_common.py
"""Building blocks shared by the test scenarios, taken from Amber's demo_experiments."""

import os
import random

import amber
from amber import backscatter, capacitor, controller, energy, propagation, radiodevices

SOLAR_TRACE = os.path.join(os.path.dirname(amber.__file__), "..", "demo_experiments", "threehundred_seconds_stable.xlsx")
FREQ_HZ = 924e6
PATHLOSS = "macro"
R_SERIES = 5e3

CONTROLLER_PARAMS = controller.ControllerParams(
    currents=controller.CurrentsA(listening=1.4e-4, sensing=0.512e-3, processing=1.28e-3, transmitting=5e-3),
    durations_ms=controller.DurationsMs(listening=5, sensing=2, processing=5, transmitting=15),
    thresholds_v=controller.VoltageThresholdsV(low=1.3, high=1.7),
)


def cap_params(capacitance: float) -> capacitor.CapacitorParams:
    return capacitor.CapacitorParams(dt=1e-3, R_series=R_SERIES, R_leakage=0.1e6, C=capacitance)


def three_sector_bs(beamwidth_deg: float = 65, sensitivity_dbm: float = -100) -> radiodevices.BaseStation:
    return radiodevices.BaseStation(
        id=0, x=0, y=0, site_radius=2,
        sectors=[radiodevices.Sector(azimuth_deg=a, beamwidth_deg=beamwidth_deg, power=46, antenna_gain_dbi=15,
                                     sensitivity_dbm=sensitivity_dbm, height=25)
                 for a in (0, 120, 240)])


def build_shared(env, bs, groups):
    """Energy source plus one coverage map per energy mode, as the demos build them.

    groups: list of (energy_mode, nodes). Returns {node_id: (node, downlink, uplink, coverage)}."""
    energy_source = energy.EnvEnergySource(env=env, file_path=SOLAR_TRACE, column="V_IM", resistance=R_SERIES)
    per_node = {}
    for mode, nodes in groups:
        cov = propagation.CoverageMap(base_stations=[bs], nodes=nodes, freq_hz=FREQ_HZ, pathloss_model=PATHLOSS, los=True,
                                      node_energy_mode=mode, node_ext_power_fn=lambda node: energy_source.ext_power,
                                      combine_mode="max")
        dl = cov.compute_bs_to_point(nodes)
        cov.calculate_node_power(nodes, dl)
        ul = cov.compute_point_to_bs(nodes)
        for node in nodes:
            per_node[node.id] = (node, dl, ul, cov)
    return per_node


def node_rng(seed, node_id) -> random.Random:
    """The node's own random generator, the same in every process that builds the node."""
    return random.Random(f"{seed}/{node_id}")


def build_node(env, shared, node_id, capacitance, seed, module_cls=backscatter.BackscatterModule):
    """One node's Capacitor, BackscatterModule and Controller, in the demos' order, drawing from
    the node's own random generator."""
    node, dl, ul, cov = shared[node_id]
    rng = node_rng(seed, node_id)
    cap = capacitor.Capacitor(env=env, id=node.id, params=cap_params(capacitance), initial_voltage=0.0)
    bsm = module_cls(env=env, node=node, bs_processes=[], uplink_results=ul, downlink_results=dl, rng=rng)
    controller.Controller(env=env, capacitor_ctrl=cap, node=node, params=CONTROLLER_PARAMS, backscatter=bsm,
                          coverage_map=cov, downlink_results=dl, rng=rng)
    return bsm
