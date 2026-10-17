# tests/test_phased_environment.py
"""Checks the event order of PhasedEnvironment on events of the same time."""

import unittest

import simpy

from amber.hosts import READER_PHASE, PhasedEnvironment


class PhasedEnvironmentTest(unittest.TestCase):
    def test_lower_phase_first_at_the_same_time(self):
        # Plain SimPy would run the node first: its timeout was scheduled at 0, the Reader's at 2.
        env = PhasedEnvironment()
        order = []

        def node():
            yield env.timeout(5)
            order.append(("node", env.now))

        def reader():
            yield env.timeout(2)
            yield env.timeout(3)
            order.append(("reader", env.now))

        with env.in_phase(3):
            env.process(node())
        with env.in_phase(READER_PHASE):
            env.process(reader())
        env.run(until=10)
        self.assertEqual(order, [("reader", 5), ("node", 5)])

    def test_reader_handles_a_transmission_before_the_next_node(self):
        # Plain SimPy would run the later node before "node after": its timeout was scheduled first.
        env = PhasedEnvironment()
        order = []

        def reader():
            try:
                yield env.timeout(100)
            except simpy.Interrupt as interrupt:
                order.append(("reader", env.now, interrupt.cause))

        def node(other):
            yield env.timeout(5)
            other.interrupt("receive_data")
            order.append(("node", env.now))
            yield env.timeout(0)
            order.append(("node after", env.now))

        def later_node():
            yield env.timeout(5)
            order.append(("later node", env.now))

        with env.in_phase(READER_PHASE):
            reader_process = env.process(reader())
        with env.in_phase(2):
            env.process(node(reader_process))
        with env.in_phase(3):
            env.process(later_node())
        env.run(until=10)
        self.assertEqual(
            order,
            [("node", 5), ("reader", 5, "receive_data"), ("node after", 5), ("later node", 5)],
        )

    def test_interrupt_takes_the_phase_of_the_interrupted_process(self):
        # Plain SimPy, or the phase of the interrupting Reader, would run the interrupt first.
        env = PhasedEnvironment()
        order = []

        def interrupted():
            try:
                yield env.timeout(100)
            except simpy.Interrupt as interrupt:
                order.append(("phase 3", interrupt.cause))

        def reader(other):
            yield env.timeout(5)
            other.interrupt("command")

        def node():
            yield env.timeout(5)
            order.append(("phase 2", None))

        with env.in_phase(3):
            interrupted_process = env.process(interrupted())
        with env.in_phase(READER_PHASE):
            env.process(reader(interrupted_process))
        with env.in_phase(2):
            env.process(node())
        env.run(until=10)
        self.assertEqual(order, [("phase 2", None), ("phase 3", "command")])

    def test_new_process_takes_the_phase_it_is_created_in(self):
        # Plain SimPy would run the child last: its timeout was scheduled at 1, the others' at 0.
        env = PhasedEnvironment()
        order = []

        def wait(name, delay):
            yield env.timeout(delay)
            order.append(name)

        def parent():
            yield env.timeout(1)
            env.process(wait("child", 1))

        with env.in_phase(2):
            env.process(wait("phase 2", 2))
        with env.in_phase(3):
            env.process(parent())
        with env.in_phase(4):
            env.process(wait("phase 4", 2))
        env.run(until=10)
        self.assertEqual(order, ["phase 2", "child", "phase 4"])

    def test_refuses_an_event_of_unknown_phase(self):
        env = PhasedEnvironment()
        with self.assertRaises(RuntimeError):
            env.timeout(1)


if __name__ == "__main__":
    unittest.main()
