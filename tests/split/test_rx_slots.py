# tests/split/test_rx_slots.py
"""Checks the RX slots a command advertises: those after it up to the next TX entry, since the next
command replaces the slots a node holds. A slot past that could be picked but never used."""

import os
import unittest

import simpy

from amber import bsengine, radiodevices, scenario
from amber.split.hosts import build_one_process

DEMO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "demo_experiments")


def load_demo(name):
    return scenario.load(os.path.join(DEMO_DIR, name, name + ".toml"), {"run.seed": 12345})


class Recorder:
    """Stands in for a node and keeps every command it receives: (time, cmd, rx_slots)."""

    def __init__(self, node_id):
        self.node = radiodevices.Node(id=node_id, x=0, y=10, height=1.5)
        self.commands = []

    def receive_command(self, cmd, bs_id, data=None):
        self.commands.append((self.env.now, cmd, (data or {}).get("rx_slots")))


def commands(schedule, until, loop=True):
    env = simpy.Environment()
    node = Recorder(0)
    node.env = env
    bs = radiodevices.BaseStation(id=0, x=0, y=0, site_radius=2, sectors=[])
    bsengine.BSBehavior(env=env, base_station=bs, schedule=schedule, backscatter_modules=[node], loop=loop)
    env.run(until=until)
    return node.commands


TX = ("tx", 5, "cmd", {"target": -1, "cmd": "send_data"})
RX = ("rx", 10, "listen")


class StaticScheduleTest(unittest.TestCase):
    def test_slots_of_its_own_frame(self):
        self.assertEqual(commands([TX, RX, RX], 60),
                         [(0, "send_data", [(5, 15), (15, 25)]), (25, "send_data", [(30, 40), (40, 50)]),
                          (50, "send_data", [(55, 65), (65, 75)])])

    def test_slots_stop_at_the_next_command(self):
        send_id = ("tx", 5, "cmd", {"target": -1, "cmd": "send_id"})
        schedule = [send_id, RX, RX, ("tx", 5, "ack_slot", {"cmd": "ack"}), TX, RX]
        self.assertEqual(commands(schedule, 40), [(0, "send_id", [(5, 15), (15, 25)]), (30, "send_data", [(35, 45)])])

    def test_command_last_in_the_frame_gets_the_next_frames_slots(self):
        self.assertEqual(commands([RX, RX, TX], 30), [(20, "send_data", [(25, 35), (35, 45)])])

    def test_a_schedule_that_does_not_loop_ends_with_it(self):
        self.assertEqual(commands([RX, TX], 30, loop=False), [(10, "send_data", [])])


class DemoTest(unittest.TestCase):
    """In Amber's demos every advertised slot ends before the Reader's next command."""

    def check(self, name, slots_per_command):
        env, bss, bsms = build_one_process(load_demo(name))
        seen = []
        for bsm in bsms:
            receive = bsm.receive_command

            def record(cmd, bs_id, data=None, receive=receive, bsm=bsm):
                seen.append((bsm.env.now, list((data or {}).get("rx_slots", []))))
                receive(cmd, bs_id, data)

            bsm.receive_command = record
        env.run(until=5000)
        command_times = sorted({t for t, _ in seen})
        self.assertGreater(len(command_times), 10)
        for t, slots in seen:
            later = [c for c in command_times if c > t]
            self.assertIn(len(slots), slots_per_command)
            if later:
                self.assertLessEqual(slots[-1][1], later[0])

    def test_broadcast(self):
        self.check("broadcast", {2})

    def test_broadcast_sic(self):
        self.check("broadcast_sic", {3})

    def test_unicast(self):
        self.check("unicast", {1})

    def test_custom_protocol(self):
        params = load_demo("custom_protocol").protocol.params
        self.check("custom_protocol", set(range(params["min_slots"], params["max_slots"] + 1)))


if __name__ == "__main__":
    unittest.main()
