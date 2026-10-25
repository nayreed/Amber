# tests/test_scenario.py
"""Checks amber.scenario: seeds, Tag placement and settings, and that a file that is not a valid
scenario is refused with a message naming the key."""

import copy
import math
import os
import sys
import tempfile
import unittest

from amber import iq, scenario
from amber.backscatter import AiotTag
from amber.hosts import SHARED_PHASE, PhasedEnvironment

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIO_DIR = os.path.join(HERE, "scenarios")
DEMO_DIR = os.path.join(HERE, "..", "demo_experiments")
BASE = os.path.join(SCENARIO_DIR, "single_node.toml")

sys.path.insert(0, HERE)
from helpers import SHORT_MS  # noqa: E402


def run(s):
    """The Reader session and the Tags' AiotTags after a run."""
    with tempfile.TemporaryDirectory() as out:
        reader, tags = iq.run(s, out)
    return reader, [tag.bsm for tag in tags]


def base_data():
    with open(BASE, "rb") as f:
        return tomllib.load(f)


class SeedTest(unittest.TestCase):
    def test_a_file_without_seed_draws_one(self):
        demo = os.path.join(DEMO_DIR, "broadcast", "broadcast.toml")
        a, b = scenario.load(demo), scenario.load(demo)
        self.assertTrue(a.seed_drawn)
        self.assertIsInstance(a.SEED, int)
        self.assertNotEqual(a.SEED, b.SEED)
        fixed = scenario.load(demo, {"run.seed": 5})
        self.assertFalse(fixed.seed_drawn)
        self.assertEqual(fixed.SEED, 5)

    def test_each_tag_draws_from_its_own_generator(self):
        import random

        def tx(seed):
            return run(scenario.load(BASE, {"run.seed": seed, "run.sim_time_ms": SHORT_MS}))[1][0].tx_records

        random.seed(1)
        first = tx(41)
        self.assertGreater(len(first), 0)
        random.seed(2)  # the global generator does not matter
        self.assertEqual(tx(41), first)
        self.assertNotEqual(tx(42), first)

    def test_the_controller_has_its_own_generator(self):
        import random
        s = scenario.load(BASE, {"run.seed": 41})
        env = PhasedEnvironment()
        with env.in_phase(SHARED_PHASE):
            shared = s.build_shared(env)
        with env.in_phase(2):
            bsm = s.build_tag(env, shared, 0)
        self.assertIsInstance(bsm, AiotTag)
        self.assertIsNotNone(bsm.controller)
        self.assertIsInstance(bsm.controller.rng, random.Random)


class PlacementTest(unittest.TestCase):
    def load(self, data):
        return scenario.Scenario(BASE, data)

    def test_ring_layout(self):
        data = base_data()
        data["tag_group"] = [{"energy_mode": "hybrid", "count": 30,
                              "layout": {"kind": "ring", "r_min": 5.0, "r_max": 40.0, "seed": 3, "center": [10, -5]}}]
        s = self.load(data)
        self.assertEqual(s.NODE_IDS, list(range(30)))
        nodes = [parts.node for parts in shared_of(s).values()]
        for node in nodes:
            self.assertTrue(5.0 <= math.hypot(node.x - 10, node.y + 5) <= 40.0 + 1e-9)
        again = [parts.node for parts in shared_of(self.load(copy.deepcopy(data))).values()]
        self.assertEqual([(n.x, n.y) for n in nodes], [(n.x, n.y) for n in again])

    def test_layout_without_seed_follows_the_run_seed(self):
        data = base_data()
        data["tag_group"] = [{"energy_mode": "hybrid", "count": 3, "layout": {"kind": "ring", "r_min": 1, "r_max": 9}}]

        def places(seed):
            d = copy.deepcopy(data)
            d["run"]["seed"] = seed
            s = self.load(d)
            return [(p.node.x, p.node.y) for p in shared_of(s).values()]

        self.assertEqual(places(1), places(1))
        self.assertNotEqual(places(1), places(2))

    def test_ids_and_order(self):
        data = base_data()
        data["tag_group"] = [{"energy_mode": "wpt", "positions": [[0, 10], [0, 20]], "ids": [7, 3]},
                             {"energy_mode": "hybrid", "positions": [[0, 30], [0, 40]]}]
        self.assertEqual(self.load(data).NODE_IDS, [7, 3, 8, 9])

    def test_group_settings_merge_over_the_defaults(self):
        data = base_data()
        data["tag_group"] = [{"energy_mode": "hybrid", "positions": [[0, 10]], "efficiency": 0.5,
                              "capacitor": {"C": 100e-6}, "controller": {"thresholds_v": {"high": 1.9}}},
                             {"energy_mode": "hybrid", "positions": [[0, 20]]}]
        s = self.load(data)
        shared = shared_of(s)
        first, second = shared[0], shared[1]
        self.assertEqual((first.node.efficiency, second.node.efficiency), (0.5, 0.7))
        self.assertEqual((first.group.cap_params.C, first.group.cap_params.R_series), (100e-6, 5e3))
        self.assertEqual(second.group.cap_params.C, 200e-6)
        self.assertEqual((first.group.controller_params.thresholds_v.high,
                          first.group.controller_params.thresholds_v.low), (1.9, 1.3))
        self.assertEqual(second.group.controller_params.thresholds_v.high, 1.7)


class LinkTest(unittest.TestCase):
    """The [phy] table is a LinkConfig with every key required; [reader] holds the receiver options."""

    def test_phy_is_required(self):
        data = base_data()
        del data["phy"]
        with self.assertRaisesRegex(ValueError, "phy is required"):
            scenario.Scenario(BASE, data)

    def test_every_phy_key_is_required(self):
        for key in list(base_data()["phy"]):
            data = base_data()
            del data["phy"][key]
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, rf"{key} is required"):
                scenario.Scenario(BASE, data)

    def test_phy_values_reach_the_link_config(self):
        data = base_data()
        data["phy"].update({"m_chip_symb": 12, "r_sfs": [1, 2]})
        s = scenario.Scenario(BASE, data)
        self.assertEqual((s.phy.m_chip_symb, s.phy.r_sfs), (12, (1, 2)))

    def test_reader_options(self):
        s = scenario.load(BASE)
        self.assertEqual((s.enable_sic, s.reader_noise_figure_db), (True, 6.0))
        s = scenario.load(BASE, {"reader.enable_sic": False, "reader.noise_figure_db": 9})
        self.assertEqual((s.enable_sic, s.reader_noise_figure_db), (False, 9.0))

    def test_every_tag_has_an_uplink(self):
        s = scenario.load(os.path.join(SCENARIO_DIR, "registration.toml"))
        links = s.iq_links()
        self.assertEqual(sorted(links), s.NODE_IDS)
        for sector, _shift, power_dbm in links.values():
            self.assertTrue(0 <= sector < 3)
            self.assertTrue(-200 < power_dbm < 0)


def shared_of(s):
    """What the scenario's Tags share, built as a host builds it."""
    env = PhasedEnvironment()
    with env.in_phase(SHARED_PHASE):
        return s.build_shared(env)


class ErrorTest(unittest.TestCase):
    """Each mistake is refused, with the key it is about in the message."""

    def refused(self, change, message):
        data = base_data()
        change(data)
        with self.assertRaisesRegex(ValueError, message):
            scenario.Scenario(BASE, data)

    def test_mistakes(self):
        def drop(*keys):
            def change(data):
                table = data
                for key in keys[:-1]:
                    table = table[key]
                del table[keys[-1]]
            return change

        def set_(value, *keys):
            def change(data):
                table = data
                for key in keys[:-1]:
                    table = table[key]
                table[keys[-1]] = value
            return change

        def group(**values):
            """Place the first group's Tags by values instead of by positions."""
            def change(data):
                del data["tag_group"][0]["positions"]
                data["tag_group"][0].update(values)
            return change

        cases = [
            (set_({}, "runs"), "unknown table runs"),
            (drop("run", "sim_time_ms"), r"run\.sim_time_ms is required"),
            (set_(0, "run", "sim_time_ms"), r"run\.sim_time_ms: must be at least 1"),
            (set_("7", "run", "seed"), r"run\.seed: must be an integer"),
            (set_(1, "run", "speed"), "run has no key speed"),
            (drop("reader", "protocol"), r"reader\.protocol is required"),
            (set_("nope", "reader", "protocol"), "unknown protocol 'nope'"),
            (set_(4, "reader", "params", "slots"), "registration has no parameter slots"),
            (set_(16, "reader", "params", "occasions_exp"), r"occasions_exp must be within \[0, 15\]"),
            (set_(1.5, "reader", "params", "occasions_exp"), "occasions_exp must be an integer"),
            (set_(1, "reader", "loop"), "reader has no key loop"),
            # keys a scenario does not have
            (set_(True, "reader", "noise_figure_db"), r"reader\.noise_figure_db: must be a number"),
            (set_(3.0, "reader", "required_sinr_db"), "reader has no key required_sinr_db"),
            (set_(0.9, "reader", "cancellation_factor"), "reader has no key cancellation_factor"),
            (set_(100e6, "reader", "bandwidth_hz"), "reader has no key bandwidth_hz"),
            (set_(80, "phy", "r2d_kbit"), "phy has no key r2d_kbit"),
            (set_(3, "phy", "m_chip_symb"), r"\[phy\]"),
            (lambda d: d["base_station"].append({**d["base_station"][0], "id": 5}), "exactly one base station"),
            (set_("yes", "reader", "enable_sic"), r"reader\.enable_sic: must be true or false"),
            (drop("base_station"), "base_station is required"),
            (set_([], "base_station"), r"base_station: must be one or more \[\[base_station\]\] tables"),
            (lambda d: d["base_station"][0].pop("x"), r"base_station\[0\]\.x is required"),
            (lambda d: d["base_station"][0]["sectors"][1].pop("beamwidth_deg"),
             r"base_station\[0\]\.sectors\[1\]\.beamwidth_deg is required"),
            (lambda d: d["base_station"][0]["sectors"][0].update(gain=3), r"sectors\[0\] has no key gain"),
            (lambda d: d["base_station"].append(dict(d["base_station"][0])), "two base stations have the same id"),
            (set_("fast", "radio", "los"), r"radio\.los: must be true or false"),
            (set_("missing.xlsx", "energy", "trace"), r"energy\.trace: no file .*missing\.xlsx"),
            (drop("energy"), r"tag_group\[0\]\.energy_mode: hybrid Tags need an \[energy\] table"),
            (lambda d: d["tag_group"][0].pop("energy_mode"), r"tag_group\[0\]\.energy_mode is required"),
            (lambda d: d["tag_group"][0].update(energy_mode="solar"), "must be one of wpt, external, hybrid"),
            (lambda d: d["tag_group"][0].update(heigth=2), r"tag_group\[0\] has no key heigth"),
            (lambda d: d["tag_group"][0].update(count=3), "give either positions or count and layout"),
            (lambda d: d["tag_group"][0].update(positions=[[1, 2, 3]]), r"positions: must be a list of one or more"),
            (lambda d: d["tag_group"][0].update(ids=[0, 1]), r"ids: must be 1 integers"),
            (lambda d: d["tag_group"].append({"energy_mode": "wpt", "positions": [[1, 1]], "ids": [0]}),
             "Tag id 0 is used twice"),
            (group(count=2, layout={"kind": "grid", "r_min": 0, "r_max": 1}), r"layout\.kind: must be ring"),
            (group(count=2), r"tag_group\[0\]\.layout is required"),
            (group(count=2, layout={"kind": "ring", "r_min": 5, "r_max": 1}),
             r"layout\.r_min: must be at least 0 and at most"),
            (drop("tag_defaults", "controller", "currents", "sensing"),
             r"tag_group\[0\]\.controller\.currents\.sensing is required"),
            (set_(1e-4, "tag_defaults", "capacitor", "Cap"), r"capacitor has no key Cap"),
            (set_([[0, 0]], "tag_defaults", "positions"), "a placement belongs in a tag_group"),
        ]
        for change, message in cases:
            with self.subTest(message):
                self.refused(change, message)

    def test_invalid_toml(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write("[run\n")
        try:
            with self.assertRaisesRegex(ValueError, f.name):
                scenario.load(f.name)
        finally:
            os.unlink(f.name)

    def test_overrides(self):
        self.assertEqual(scenario.load(BASE, {"reader.params.occasions_exp": 3}).protocol.params["occasions_exp"], 3)
        self.assertEqual(scenario.load(BASE, {"tag_group.0.positions": [[1, 1], [2, 2]]}).NODE_IDS, [0, 1])
        with self.assertRaisesRegex(ValueError, "tag_group has no entry 3"):
            scenario.load(BASE, {"tag_group.3.count": 2})
        with self.assertRaisesRegex(ValueError, "is inside a value"):
            scenario.load(BASE, {"run.seed.x": 1})


class NetworkTest(unittest.TestCase):
    """[network] names who sends R2D, who receives D2R and who sends the carrier; it maps onto the
    four topologies of the Rel-19 study."""

    def network(self, **values):
        data = base_data()
        data["network"] = values
        return scenario.Scenario(BASE, data).network

    def test_default_is_a_standalone_reader(self):
        net = scenario.Scenario(BASE, base_data()).network
        self.assertEqual((net.cell, net.r2d, net.d2r, net.cw, net.relay), (None, "reader", "reader", "reader", False))
        self.assertIsNone(net.topology)

    def test_topologies(self):
        cell = "n28-25prb"
        self.assertEqual(self.network(cell=cell, r2d="gnb", d2r="gnb", cw="cw-node-ul").topology, 1)
        self.assertEqual(self.network(cell=cell, r2d="ue", d2r="ue", cw="ue-ul", relay=True).topology, 2)
        self.assertEqual(self.network(cell=cell, r2d="ue", d2r="gnb", cw="ue-ul").topology, 3)
        self.assertEqual(self.network(cell=cell, r2d="gnb", d2r="ue", cw="gnb-dl").topology, 3)
        self.assertEqual(self.network(cell=cell, r2d="ue", d2r="ue", cw="ue-ul").topology, 4)
        self.assertIsNone(self.network(cell=cell).topology)

    def test_refused(self):
        cases = [
            (dict(cell="n78"), r"network\.cell: must be one of 'n28-25prb'"),
            (dict(r2d="gnb"), r"network\.r2d: a standalone Reader both sends R2D and receives D2R"),
            (dict(cell="n28-25prb", r2d="gnb", d2r="gnb"), r"network\.cw: \"reader\" is the standalone Reader's own carrier"),
            (dict(r2d="gnb", d2r="gnb", cw="gnb-dl"), r"network\.cell: a gNB or a UE in the A-IoT link needs"),
            (dict(cell="n28-25prb", r2d="gnb", d2r="gnb", cw="cw-node-ul", relay=True), r"network\.relay: only a UE"),
            (dict(cell="n28-25prb", relay="yes"), r"network\.relay: must be true or false"),
            (dict(cells="n28-25prb"), "network has no key cells"),
        ]
        for values, message in cases:
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, message):
                self.network(**values)


class HostsTest(unittest.TestCase):
    """[network.hosts] places the nodes of a 5G room on machines; a node it does not name runs with
    the hub, and it may name only nodes the room has."""

    T1 = dict(cell="n28-25prb", r2d="gnb", d2r="gnb", cw="gnb-dl")

    def network(self, hosts, **values):
        data = base_data()
        data["network"] = dict(values or self.T1, hosts=hosts)
        return scenario.Scenario(BASE, data).network

    def test_one_machine_without_it(self):
        data = base_data()
        data["network"] = dict(self.T1)
        net = scenario.Scenario(BASE, data).network
        self.assertEqual(net.hosts, ())
        self.assertIsNone(net.host("gnb"))
        self.assertEqual(self.network({}).hosts, ())

    def test_nodes_not_named_run_with_the_hub(self):
        net = self.network({"tags": "10.0.0.2", "hub": "10.0.0.3", "gnb": "gnb-host"})
        self.assertEqual(net.hosts, (("hub", "10.0.0.3"), ("gnb", "gnb-host"), ("tags", "10.0.0.2")))
        self.assertEqual([net.host(n) for n in ("hub", "gnb", "ue", "tags")],
                         ["10.0.0.3", "gnb-host", "10.0.0.3", "10.0.0.2"])

    def test_each_node_of_each_room(self):
        cw_node = dict(cell="n28-25prb", r2d="ue", d2r="ue", cw="cw-node-ul")
        self.assertEqual(self.network({"hub": "a", "cw": "b"}, **cw_node).host("cw"), "b")
        self.assertEqual(self.network({"hub": "a", "reader": "b"}, cell="n28-25prb").host("reader"), "b")

    def test_refused(self):
        cases = [
            ({"gnb": "10.0.0.1"}, None, r"network\.hosts\.hub: is required"),
            ({"hub": ""}, None, r"network\.hosts\.hub: must be a host name or an address, got ''"),
            ({"hub": "10.0.0.1 "}, None, r"network\.hosts\.hub: must be a host name or an address"),
            ({"hub": 3}, None, r"network\.hosts\.hub: must be a string"),
            ({"hub": "a", "sink": "b"}, None, r"network\.hosts has no key sink"),
            ({"hub": "a", "cw": "b"}, None, r"network\.hosts\.cw: this room has no cw node"),
            ({"hub": "a", "reader": "b"}, None, r"network\.hosts\.reader: this room has no reader node"),
            ({"hub": "a"}, dict(r2d="reader"), r"network\.hosts\.hub: only a 5G room"),
        ]
        for hosts, values, message in cases:
            with self.subTest(hosts=hosts), self.assertRaisesRegex(ValueError, message):
                self.network(hosts, **(values or {}))


if __name__ == "__main__":
    unittest.main()
