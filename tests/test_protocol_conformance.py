# tests/test_protocol_conformance.py
"""Checks every protocol a scenario can name, found by asking the MAC library, so a new protocol is
tested as soon as the library has it: its defaults pass its own check on the default link, its
Reader starts and sends an R2D, and its table, if it keeps one, has a header for every row.

The protocols are in OAI's C MAC (openair2/LAYER2/AIOT_MAC). The Reader draws only the Security
Parameter of its pagings (38.391 V19.4.0 5.2), from its own fixed-seed generator: a run on one
machine and the Reader container must agree, and the C protocols take no other random numbers
(only the devices draw, from the generator their caller gives them).
"""

import unittest

from amber import link, mac, protocols


class ConformanceTest(unittest.TestCase):
    def test_protocols_are_found(self):
        names = protocols.names()
        for name in ("broadcast", "unicast", "registration", "adaptive_aloha", "gen2_q"):
            self.assertIn(name, names)

    def test_defaults_pass_check(self):
        for name in protocols.names():
            with self.subTest(name):
                protocol = protocols.load(name, {}, [0, 1, 2])
                self.assertEqual(set(protocol.params), set(mac.protocol_params(name)))
                self.assertEqual(protocol.name, name)
                self.assertEqual(protocol.node_ids, [0, 1, 2])

    def test_each_reader_sends(self):
        for name in protocols.names():
            with self.subTest(name):
                reader = protocols.load(name, {}, [1, 2]).reader(link.DEFAULT_LINK)
                r2d = reader.next_r2d(0)
                self.assertIsNotNone(r2d)
                msg, bits, earliest = r2d
                self.assertEqual(earliest, 0)
                self.assertEqual(mac.pack_r2d(msg), bits)

    def test_protocol_pagings_carry_a_security_parameter(self):
        """Every paging a protocol sends has SPPI 1 and 128 bits, the same on every run."""
        def first_paging_bits(seed=None):
            reader = protocols.load("broadcast", {}, [1, 2]).reader(link.DEFAULT_LINK)
            if seed is not None:
                reader.seed(seed)
            msg, bits, _ = reader.next_r2d(0)
            self.assertIn(msg.kind, (mac.PAGING_CBRA, mac.PAGING_CFA))
            self.assertEqual(msg.has_security_param, 1)
            sec = list(msg.security_param)
            self.assertEqual(len(sec), mac.SECURITY_PARAM_BITS)
            self.assertTrue(set(sec) <= {0, 1})
            self.assertTrue(0 < sum(sec) < len(sec))
            self.assertEqual(mac.parse_r2d(bits).has_security_param, 1)
            self.assertEqual(list(mac.parse_r2d(bits).security_param), sec)
            return sec
        a = first_paging_bits()
        self.assertEqual(first_paging_bits(), a)      # nothing random between runs
        self.assertNotEqual(first_paging_bits(7), a)  # until the Reader is given a seed

    def test_outputs_have_a_column_for_every_value(self):
        for name in protocols.names():
            with self.subTest(name):
                p = protocols.load(name, {}, [0])
                p.reader(link.DEFAULT_LINK)
                for file, (columns, rows) in p.outputs().items():
                    self.assertTrue(file.endswith(".csv"))
                    self.assertTrue(all(len(row) == len(columns) for row in rows))

    def test_struct_layouts_match_the_library(self):
        """amber.mac checks its copies of the C structures when it loads the library."""
        for name, cls in mac._STRUCTS.items():
            with self.subTest(name):
                self.assertEqual(mac._L().aiot_mac_struct_size(name.encode()), mac.C.sizeof(cls))


if __name__ == "__main__":
    unittest.main()
