# tests/test_coexist.py
"""Checks amber.coexist: the TR 38.901 path loss models against hand-worked values of their formulas,
the model TR 38.769 Table 6.6.2.1-1 gives each pair, and the powers of a 5G room from where
[network] places the gNB and the UE."""

import math
import os
import unittest

from amber import coexist, scenario

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, "scenarios", "single_node.toml")
FC = 3.5e9
LOG_FC = math.log10(3.5)


class ModelsTest(unittest.TestCase):
    """Each model, worked by hand from TR 38.901 Table 7.4.1-1 at 3.5 GHz."""

    def test_umi(self):
        d3d = math.hypot(100, 8.5)  # h_BS 10 m, h_UT 1.5 m; d'BP = 4 * 9 * 0.5 * 3.5e9 / 3e8 = 210 m
        los = 32.4 + 21 * math.log10(d3d) + 20 * LOG_FC
        self.assertAlmostEqual(coexist.umi_db(100, FC, 10, 1.5, True), los, places=6)
        nlos = 35.3 * math.log10(d3d) + 22.4 + 21.3 * LOG_FC
        self.assertAlmostEqual(coexist.umi_db(100, FC, 10, 1.5, False), max(los, nlos), places=6)
        # beyond the breakpoint, the second LOS formula
        d3d = math.hypot(400, 8.5)
        far = 32.4 + 40 * math.log10(d3d) + 20 * LOG_FC - 9.5 * math.log10(210.0 ** 2 + 8.5 ** 2)
        self.assertAlmostEqual(coexist.umi_db(400, FC, 10, 1.5, True), far, places=4)

    def test_inh_office(self):
        d3d = math.hypot(10, 1.5)
        los = 32.4 + 17.3 * math.log10(d3d) + 20 * LOG_FC
        self.assertAlmostEqual(coexist.inh_office_db(10, FC, 3, 1.5, True), los, places=6)
        nlos = 38.3 * math.log10(d3d) + 17.30 + 24.9 * LOG_FC
        self.assertAlmostEqual(coexist.inh_office_db(10, FC, 3, 1.5, False), max(los, nlos), places=6)

    def test_inf_dh(self):
        d3d = math.hypot(10, 6.5)
        los = 31.84 + 21.50 * math.log10(d3d) + 19.00 * LOG_FC
        self.assertAlmostEqual(coexist.inf_dh_db(10, FC, 8, 1.5, True), los, places=6)
        dh = 33.63 + 21.9 * math.log10(d3d) + 20 * LOG_FC
        self.assertAlmostEqual(coexist.inf_dh_db(10, FC, 8, 1.5, False), max(los, dh), places=6)

    def test_o2i_high_loss(self):
        # TR 38.901 Table 7.4.3-2 at 0.9 GHz: IRR glass 23.27 dB, concrete 8.6 dB
        expected = 5 - 10 * math.log10(0.7 * 10 ** (-23.27 / 10) + 0.3 * 10 ** (-8.6 / 10))
        self.assertAlmostEqual(coexist.o2i_high_db(0.9e9), expected, places=6)

    def test_the_distance_never_falls_under_a_metre(self):
        self.assertEqual(coexist.inh_office_db(0, FC, 1.5, 1.5, True), coexist.inh_office_db(0.5, FC, 1.5, 1.5, True))


class PairsTest(unittest.TestCase):
    """The model of each pair, as TR 38.769 Table 6.6.2.1-1 (D1T1) has it."""

    G, U, R, T = (0.0, 0.0, 25.0), (200.0, 0.0, 1.5), (500.0, 0.0, 8.0), (510.0, 0.0, 1.5)

    def loss(self, a, sa, b, sb, ue_indoor=False, los=True):
        return coexist.pair_loss_db(a, sa, b, sb, 0.9e9, los, ue_indoor)

    def test_gnb_and_ue_uma_outdoor_and_with_o2i_indoor(self):
        uma = coexist.uma_db(200, 0.9e9, 25, 1.5, True)
        self.assertAlmostEqual(self.loss("gnb", self.G, "ue", self.U), max(uma, coexist.MCL_DB))
        self.assertAlmostEqual(self.loss("ue", self.U, "gnb", self.G, ue_indoor=True),
                               max(uma + coexist.o2i_high_db(0.9e9), coexist.MCL_DB))

    def test_gnb_and_aiot_nodes_uma_with_o2i(self):
        uma = coexist.uma_db(510, 0.9e9, 25, 1.5, True)
        self.assertAlmostEqual(self.loss("gnb", self.G, "tag", self.T), uma + coexist.o2i_high_db(0.9e9))

    def test_minimum_coupling_loss(self):
        self.assertEqual(self.loss("gnb", self.G, "ue", (1.0, 0.0, 1.5)), coexist.MCL_DB)

    def test_ue_and_aiot_nodes(self):
        o2i = coexist.o2i_high_db(0.9e9)
        self.assertAlmostEqual(self.loss("ue", self.U, "reader", self.R),
                               coexist.umi_db(300, 0.9e9, 8.0, 1.5, True) + o2i)
        self.assertAlmostEqual(self.loss("tag", self.T, "ue", self.U, ue_indoor=True),
                               coexist.inh_office_db(310, 0.9e9, 1.5, 1.5, True))
        self.assertAlmostEqual(self.loss("ue", self.U, "reader", self.R, ue_indoor=True),
                               coexist.inf_dh_db(300, 0.9e9, 1.5, 8.0, True))

    def test_reader_and_tag(self):
        self.assertAlmostEqual(self.loss("reader", self.R, "tag", self.T, los=False),
                               coexist.inf_dh_db(10, 0.9e9, 8.0, 1.5, False))

    def test_no_model_for_other_pairs(self):
        with self.assertRaisesRegex(ValueError, "no path loss model for the pair tag - tag"):
            self.loss("tag", self.T, "tag", self.T)


class RoomTest(unittest.TestCase):
    """[network.gnb] and [network.ue], and the powers they give."""

    def load(self, **overrides):
        return scenario.load(BASE, {"network.cell": "n28-25prb", **overrides})

    def test_no_powers_without_both_sites(self):
        self.assertIsNone(coexist.room_powers(self.load()))

    def test_sites_take_tr_38_769_defaults(self):
        net = self.load(**{"network.gnb": {"x": 300, "y": 0}, "network.ue": {"x": 50, "y": 5, "indoor": True}}).network
        self.assertEqual(net.gnb, coexist.Site(300.0, 0.0, 25.0, 46.0, 15.0))
        self.assertEqual(net.ue, coexist.Site(50.0, 5.0, 1.5, 23.0, 0.0, True))

    def test_refused(self):
        cases = [
            ({"network.gnb": {"x": 1, "y": 0}}, r"network\.ue: the gains need both"),
            ({"network.gnb": {"x": 1}, "network.ue": {"x": 1, "y": 1}}, r"network\.gnb\.y is required"),
            ({"network.gnb": {"x": 1, "y": 0, "height": 0}, "network.ue": {"x": 1, "y": 1}},
             r"network\.gnb\.height: must be above 0"),
            ({"network.gnb": {"x": 1, "y": 0, "indoor": True}, "network.ue": {"x": 1, "y": 1}},
             "network.gnb has no key indoor"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, message):
                self.load(**overrides)
        with self.assertRaisesRegex(ValueError, r"network\.gnb: only a 5G room"):
            scenario.load(BASE, {"network.gnb": {"x": 1, "y": 0}, "network.ue": {"x": 1, "y": 1}})

    def test_powers(self):
        s = self.load(**{"network.gnb": {"x": 300, "y": 0}, "network.ue": {"x": 50, "y": 80}})
        p = coexist.room_powers(s)
        gnb, ue = s.network.gnb, s.network.ue
        pl = coexist.pair_loss_db("gnb", (300, 0, 25), "ue", (50, 80, 1.5), s.freq_hz, s.los, False)
        self.assertAlmostEqual(p.gnb_at_ue, gnb.power_dbm + gnb.antenna_gain_dbi - pl)
        # the link is reciprocal: only the powers and the gains differ
        self.assertAlmostEqual(p.gnb_at_ue - p.ue_at_gnb, 46 - 23)
        self.assertEqual(set(p.tags), set(s.NODE_IDS))
        links = s.iq_links()
        for node_id, t in p.tags.items():
            self.assertEqual(t.at_reader, links[node_id][2])
            self.assertLess(t.at_gnb, t.at_reader)  # 300 m and a wall away, against a few metres
            self.assertLess(t.gnb, t.carrier)
        # the Tag's reflection at the gNB is what it reflects, over the same pair loss as the gNB's DL to it
        node_id, t = next(iter(p.tags.items()))
        node, _, reflected_dbm, _ = s.tag_radio()[node_id]
        self.assertAlmostEqual(t.gnb - t.at_gnb, gnb.power_dbm - reflected_dbm)
        # the further the gNB, the less of it everywhere
        far = coexist.room_powers(self.load(**{"network.gnb": {"x": 900, "y": 0}, "network.ue": {"x": 50, "y": 80}}))
        self.assertLess(far.gnb_at_ue, p.gnb_at_ue)
        self.assertLess(far.reader_at_gnb, p.reader_at_gnb)
        self.assertLess(far.tags[node_id].gnb, t.gnb)


if __name__ == "__main__":
    unittest.main()
