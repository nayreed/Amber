# tests/split/test_wire_rules.py
"""Checks the rules a protocol must follow to run split, each refused with a message that says what
is wrong: durations must be whole milliseconds (in one process too), commands and Tag payloads must
be plain JSON values, and a message must fit in a block."""

import unittest

import simpy

from amber import radiodevices
from amber.bsengine import BSBehavior, whole_ms_frame
from amber.split import wire
from amber.split.proxies import Delivery, Uplink


def station():
    return radiodevices.BaseStation(id=0, x=0, y=0, site_radius=2,
                                    sectors=[radiodevices.Sector(azimuth_deg=0, beamwidth_deg=360)])


class WholeMillisecondsTest(unittest.TestCase):
    def test_whole_durations_become_ints(self):
        frame = whole_ms_frame([("tx", 5.0, "cmd", {"cmd": "go"}), ("rx", 10, "slot")])
        self.assertEqual(frame, [("tx", 5, "cmd", {"cmd": "go"}), ("rx", 10, "slot")])
        self.assertTrue(all(type(e[1]) is int for e in frame))

    def test_fractional_durations_are_refused(self):
        for duration in (0.5, 10.25, "10", None, True):
            with self.subTest(duration), self.assertRaisesRegex(ValueError, "whole number of milliseconds"):
                whole_ms_frame([("tx", 1, "cmd"), ("rx", duration, "slot")])

    def test_policy_frame_refused_in_one_process(self):
        env = simpy.Environment()

        def policy(bs):
            yield [("tx", 1, "cmd", {"cmd": "go", "target": -1}), ("rx", 10, "a")]
            yield [("tx", 1, "cmd", {"cmd": "go", "target": -1}), ("rx", 10.5, "b")]

        BSBehavior(env=env, base_station=station(), policy=policy)
        with self.assertRaisesRegex(ValueError, r"\('rx', 10.5, 'b'\).*whole number of milliseconds"):
            env.run(until=100)

    def test_static_schedule_refused(self):
        with self.assertRaisesRegex(ValueError, "whole number of milliseconds"):
            BSBehavior(env=simpy.Environment(), base_station=station(), schedule=[("tx", 2.5, "cmd")])


class PlainJsonTest(unittest.TestCase):
    def command(self, data):
        return wire.encode_deliveries([Delivery(3, "assign", 0, data)])

    def test_plain_values_pass(self):
        data = {"cmd": "assign", "target": 3, "q": 4, "w": 0.25, "ok": True, "none": None,
                "slots": [[1, 2], [3, 4]], "table": {"3": 1}, "rx_slots": [(10, 20, "data")]}
        encoded = wire.unframe(wire.frame({"cmds": self.command(data)}, 1024))["cmds"]
        self.assertEqual(wire.decode_deliveries(encoded, 3)[0].data, data)

    def test_commands_that_would_change_or_fail(self):
        import numpy as np
        for data, message in (
                ({"slots": (1, 2)}, r"data\['slots'\] is a tuple, which arrives as a list"),
                ({"table": {3: 1}}, r"data\['table'\] has the key 3 \(int\); JSON turns dict keys into strings"),
                ({"ids": {1, 2}}, r"data\['ids'\] is a set, which JSON cannot carry"),
                ({"q": np.int64(4)}, r"data\['q'\] is a int64, which JSON cannot carry"),
                ({"blob": b"x"}, r"data\['blob'\] is a bytes"),
                ({"rx_slots": [(1, 2, (3, 4))]}, r"data\['rx_slots'\]\[0\]\[2\] is a tuple")):
            with self.subTest(data), self.assertRaisesRegex(ValueError, "command 'assign' from base station 0 to node 3: " + message):
                self.command(data)

    def test_tag_payload(self):
        self.assertEqual(wire.encode_uplinks([Uplink(0, 5, {"type": "data", "payload": 7}, -70.0, 0, 0)])[0][1],
                         {"type": "data", "payload": 7})
        with self.assertRaisesRegex(ValueError, r"node 5's transmission to base station 0: payload\['samples'\] is a tuple"):
            wire.encode_uplinks([Uplink(0, 5, {"type": "data", "samples": (1, 2)}, -70.0, 0, 0)])


class BlockSizeTest(unittest.TestCase):
    def test_overflow_says_which_message_and_what_to_do(self):
        deliveries = [Delivery(i, "send_data", 0, {"cmd": "send_data", "target": i}) for i in range(20)]
        with self.assertRaisesRegex(ValueError, r"the Reader's message at 7 ms, holding 20 different commands, "
                                                r"is \d+ bytes .* 256-byte block; raise block_bytes"):
            wire.frame({"t": "ms", "ms": 7, "cmds": wire.encode_deliveries(deliveries)}, 256)
        with self.assertRaisesRegex(ValueError, r"a Tag's reply at 7 ms, holding 1 transmissions"):
            wire.frame({"t": "up", "ms": 7, "up": [[0, "x" * 300, -70.0, 0, 0]]}, 256)


if __name__ == "__main__":
    unittest.main()
