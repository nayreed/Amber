# tests/test_whole_ms.py
"""Checks that BSBehavior refuses a frame duration that is not a whole number of milliseconds: the
Reader and Tag containers advance one millisecond at a time, so such a frame could not run on OAI."""

import unittest

import simpy

from amber import radiodevices
from amber.bsengine import BSBehavior, whole_ms_frame


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

    def test_policy_frame_refused(self):
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


if __name__ == "__main__":
    unittest.main()
