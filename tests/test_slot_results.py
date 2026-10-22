# tests/test_slot_results.py
"""Checks BSBehavior.slot_results, what each listening slot of a frame held (empty slots included),
and the gen2_q protocol, which decides after every slot from it."""

import os
import tempfile
import unittest

import pandas as pd
import simpy

from amber import iq, protocols, radiodevices, scenario
from amber.bsengine import BSBehavior, SlotResult

SCENARIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")


def transmit(env, bs, at_ms, node_id, rssi_dbm=-50.0):
    """What the Reader decoded at at_ms, handed over as amber.iq hands it: a frame of node_id, or,
    for node id -1, a preamble heard but not decoded (a collision)."""
    yield env.timeout(at_ms)
    bs.received_tag_id = node_id
    bs.data_from_tag = 100 + node_id if node_id >= 0 else None
    bs.rssi_dbm = rssi_dbm
    bs.best_sector_idx = 0 if node_id >= 0 else -1
    bs.subcarrier_shift = 0
    bs.action.interrupt("receive_data")


class SlotResultsTest(unittest.TestCase):
    def frame_results(self):
        """One frame: a command, then slots A to D holding nothing, one packet, two packets that
        collided, and a packet decoded beside one that collided."""
        env = simpy.Environment()
        station = radiodevices.BaseStation(id=0, x=0, y=0, site_radius=2,
                                           sectors=[radiodevices.Sector(azimuth_deg=0, beamwidth_deg=360)])
        seen = []

        def policy(bs):
            yield [("tx", 1, "cmd", {"cmd": "go", "target": -1})] + \
                [("rx", 10, name) for name in ("A", "B", "C", "D")]
            seen.extend(bs.slot_results)

        bs = BSBehavior(env=env, base_station=station, policy=policy)
        for at_ms, node_id in ((15, 1), (25, -1), (25, -1), (35, 4), (35, -1)):
            env.process(transmit(env, bs, at_ms, node_id))
        env.run(until=60)
        return seen

    def test_every_slot_is_recorded(self):
        a, b, c, d = self.frame_results()
        self.assertEqual([(s.slot_id, s.start_ms, s.end_ms) for s in (a, b, c, d)],
                         [("A", 1, 11), ("B", 11, 21), ("C", 21, 31), ("D", 31, 41)])
        self.assertEqual((a.decoded, a.collided, a.empty, a.success, a.collision), ([], 0, True, False, False))
        self.assertEqual((b.decoded, b.collided, b.empty, b.success, b.collision), ([1], 0, False, True, False))
        # Two receptions with no node id: the Reader heard both preambles and decoded neither.
        self.assertEqual((c.decoded, c.collided, c.empty, c.success, c.collision), ([], 2, False, False, True))
        # One decoded and one that collided in the same slot (capture): decoded and collided at once.
        self.assertEqual((d.decoded, d.collided, d.empty, d.success, d.collision), ([4], 1, False, False, True))

    def test_reset_at_each_frame(self):
        env = simpy.Environment()
        station = radiodevices.BaseStation(id=0, x=0, y=0, site_radius=2,
                                           sectors=[radiodevices.Sector(azimuth_deg=0, beamwidth_deg=360)])
        counts = []

        def policy(bs):
            for n in (3, 1, 2):
                yield [("tx", 1, "cmd", {"cmd": "go", "target": -1})] + [("rx", 5, "s") for _ in range(n)]
                counts.append(len(bs.slot_results))

        BSBehavior(env=env, base_station=station, policy=policy)
        env.run(until=100)
        self.assertEqual(counts, [3, 1, 2])

    def test_static_schedule_too(self):
        env = simpy.Environment()
        station = radiodevices.BaseStation(id=0, x=0, y=0, site_radius=2,
                                           sectors=[radiodevices.Sector(azimuth_deg=0, beamwidth_deg=360)])
        bs = BSBehavior(env=env, base_station=station, loop=False,
                        schedule=[("tx", 5, "cmd", {"cmd": "go", "target": -1}), ("rx", 10, "a"), ("rx", 10, "b")])
        env.process(transmit(env, bs, 20, 7))
        env.run(until=40)
        self.assertEqual([(s.slot_id, s.decoded) for s in bs.slot_results], [("a", []), ("b", [7])])


class FakeBS:
    """What the gen2_q policy reads and writes on a BSBehavior."""

    def __init__(self):
        self.env = simpy.Environment()
        self.slot_results = []


def slot(decoded=(), collided=0):
    return SlotResult("slot", 0, 10, list(decoded), collided)


class Gen2PolicyTest(unittest.TestCase):
    def frames(self, outcomes, q0=4.0, c=0.5):
        """Drive the policy, answering each frame with the next outcome; returns the commands sent."""
        protocol = protocols.load("gen2_q", {"q0": q0, "c": c}, [0, 1])
        bs = FakeBS()
        policy = protocol.reader_args()["policy"](bs)
        commands = []
        for outcome in outcomes:
            frame = next(policy)
            self.assertEqual([e[0] for e in frame], ["tx", "rx"])
            commands.append(frame[0][3])
            bs.slot_results = [outcome]
        commands.append(next(policy)[0][3])
        return commands, bs

    def test_a_round_is_one_query_then_queryreps(self):
        commands, _ = self.frames([slot([0])] * 16)  # Q = 4: 16 slots, all successes
        self.assertEqual([c["cmd"] for c in commands], ["query"] + ["queryrep"] * 15 + ["query"])
        self.assertEqual((commands[0]["q"], commands[-1]["q"]), (4, 4))

    def test_collisions_raise_q_and_empty_slots_lower_it(self):
        commands, bs = self.frames([slot(collided=2)] * 16)
        self.assertEqual(commands[-1]["q"], 12)  # 4 + 16 * 0.5
        commands, _ = self.frames([slot()] * 16)
        self.assertEqual(commands[-1]["q"], 0)  # 4 - 16 * 0.5, floored at 0

    def test_decoded_and_collided_counts_as_collision(self):
        commands, bs = self.frames([slot([3], collided=1)] * 16, c=0.25)
        self.assertEqual(commands[-1]["q"], 8)
        self.assertEqual(bs.q_history[0][1:], (4, 1, 1))

    def test_q_stays_within_0_and_15(self):
        commands, _ = self.frames([slot(collided=2)] * 2 ** 13, q0=13.0, c=1.0)
        self.assertEqual(commands[-1]["q"], 15)

    def test_parameters_are_checked(self):
        for params, message in (({"q0": 16.0}, "q0 must be within"), ({"c": 0.0}, "c must be in"),
                                ({"slot_ms": 0}, "slot_ms must be at least 1")):
            with self.subTest(params), self.assertRaisesRegex(ValueError, message):
                protocols.load("gen2_q", params, [0])


class Gen2ScenarioTest(unittest.TestCase):
    def test_runs_and_writes_q_csv(self):
        path = os.path.join(SCENARIO_DIR, "gen2_q_iq.toml")
        s = scenario.load(path)
        with tempfile.TemporaryDirectory() as out:
            reader, _ = iq.run(s, out)
            bs = reader.host.bss[0]
            decoded = [p for p in bs.rx_packets if not p.collided]
            self.assertGreater(len(decoded), 0)
            # The Tags are registered, so they sense data instead of sending 0.
            self.assertTrue(all(p.payload != 0 for p in decoded))
            self.assertGreater(len({q for _, q, _, _ in bs.q_history}), 1)
            frame = pd.read_csv(os.path.join(out, "q.csv"))
            self.assertEqual(list(frame.columns), ["time_s", "q", "decoded", "collided"])
            self.assertEqual(len(frame), len(bs.q_history))


if __name__ == "__main__":
    unittest.main()
