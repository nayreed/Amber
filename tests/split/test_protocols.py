# tests/split/test_protocols.py
"""Checks amber.protocols: each protocol gives the schedule the scenarios had written out, checks
its parameters, and a scenario can name a protocol of its own by module path."""

import importlib
import os
import sys
import tempfile
import textwrap
import unittest

from amber import protocols
from amber.protocols.adaptive_aloha import AdaptiveAlohaNode
from amber.split.hosts import load_scenario

SCENARIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")
DEMO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "demo_experiments", "split")


def schedule_of(path):
    return load_scenario(path).SCHEDULE


class ScheduleTest(unittest.TestCase):
    """The schedules the Python scenarios list, made by the protocols from their parameters."""

    def args(self, name, params, node_ids=(0, 1, 2)):
        return protocols.load(name, params, list(node_ids)).reader_args()

    def test_broadcast(self):
        self.assertEqual(self.args("broadcast", {}),
                         {"schedule": schedule_of(os.path.join(SCENARIO_DIR, "broadcast.py")), "loop": True})
        self.assertEqual(self.args("broadcast", {"slots": 3, "rx_ms": 15})["schedule"],
                         schedule_of(os.path.join(DEMO_DIR, "broadcast_sic.py")))

    def test_unicast(self):
        self.assertEqual(self.args("unicast", {}),
                         {"schedule": schedule_of(os.path.join(SCENARIO_DIR, "unicast.py")), "loop": True})
        schedule = self.args("unicast", {}, (4, 2))["schedule"]
        self.assertEqual([step[3]["target"] for step in schedule if step[0] == "tx"], [4, 2])

    def test_registration(self):
        self.assertEqual(self.args("registration", {})["schedule"],
                         schedule_of(os.path.join(SCENARIO_DIR, "registration.py")))
        self.assertEqual(self.args("registration", {"id_slots": 2})["schedule"],
                         schedule_of(os.path.join(SCENARIO_DIR, "single_node.py")))

    def test_adaptive_aloha_uses_its_tag_class_and_a_policy(self):
        protocol = protocols.load("adaptive_aloha", {}, [0, 1])
        self.assertIs(protocol.TAG_CLASS, AdaptiveAlohaNode)
        self.assertEqual(list(protocol.reader_args()), ["policy"])

    def test_registered_from_the_start_only_where_the_protocol_needs_it(self):
        class Fake:
            state = "idle"
            nodes_registered = []

        for name, registered in (("broadcast", True), ("unicast", True), ("registration", False)):
            protocol = protocols.load(name, {}, [3, 5])
            bs, bsm = Fake(), Fake()
            protocol.setup_reader(bs)
            protocol.setup_tag(bsm)
            self.assertEqual(bs.nodes_registered, [3, 5] if registered else [], name)
            self.assertEqual(bsm.state, "registered" if registered else "idle", name)


class ParamsTest(unittest.TestCase):
    def test_defaults_and_values(self):
        protocol = protocols.load("broadcast", {"slots": 4}, [0])
        self.assertEqual(protocol.params, {"slots": 4, "tx_ms": 5, "rx_ms": 10})
        self.assertEqual(protocol.name, "broadcast")

    def test_unknown_parameter(self):
        with self.assertRaisesRegex(ValueError, "broadcast has no parameter slot "):
            protocols.load("broadcast", {"slot": 4}, [0])

    def test_wrong_kind(self):
        for value in (2.0, True, "2"):
            with self.assertRaisesRegex(ValueError, "slots must be an integer"):
                protocols.load("broadcast", {"slots": value}, [0])
        # A float parameter takes an integer too.
        self.assertEqual(protocols.load("adaptive_aloha", {"smoothing": 1}, [0]).params["smoothing"], 1)

    def test_out_of_range(self):
        with self.assertRaisesRegex(ValueError, "slots must be at least 1"):
            protocols.load("broadcast", {"slots": 0}, [0])
        with self.assertRaisesRegex(ValueError, "start_slots must be within"):
            protocols.load("adaptive_aloha", {"start_slots": 40}, [0])
        with self.assertRaisesRegex(ValueError, "smoothing must be in"):
            protocols.load("adaptive_aloha", {"smoothing": 0.0}, [0])


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        sys.path.insert(0, self.dir.name)

    def tearDown(self):
        sys.path.remove(self.dir.name)
        for name in [m for m in sys.modules if m.startswith("my_protocols")]:
            del sys.modules[name]
        self.dir.cleanup()

    def write(self, name, source):
        package = os.path.join(self.dir.name, "my_protocols")
        os.makedirs(package, exist_ok=True)
        open(os.path.join(package, "__init__.py"), "a").close()
        with open(os.path.join(package, name + ".py"), "w") as f:
            f.write(textwrap.dedent(source))
        importlib.invalidate_caches()

    def test_unknown_protocol(self):
        with self.assertRaisesRegex(ValueError, "unknown protocol 'nope'"):
            protocols.load("nope", {}, [0])
        with self.assertRaisesRegex(ValueError, "unknown protocol 'my_protocols.nope'"):
            protocols.load("my_protocols.nope", {}, [0])

    def test_own_protocol_by_module_path(self):
        self.write("poll", """
            from amber.protocols import Protocol

            class Poll(Protocol):
                PARAMS = {"rx_ms": 10}

                def reader_args(self):
                    return {"schedule": [("tx", 5, "cmd", {"target": -1, "cmd": "send_data"}),
                                         ("rx", self.params["rx_ms"], "listen")], "loop": True}

            PROTOCOL = Poll
        """)
        protocol = protocols.load("my_protocols.poll", {"rx_ms": 20}, [0])
        self.assertEqual(protocol.name, "my_protocols.poll")
        self.assertEqual(protocol.reader_args()["schedule"][1], ("rx", 20, "listen"))

    def test_module_without_protocol(self):
        self.write("empty", "X = 1\n")
        with self.assertRaisesRegex(ValueError, "does not define PROTOCOL"):
            protocols.load("my_protocols.empty", {}, [0])

    def test_missing_import_inside_a_protocol_is_not_hidden(self):
        self.write("broken", "import module_that_does_not_exist\n")
        with self.assertRaises(ModuleNotFoundError):
            protocols.load("my_protocols.broken", {}, [0])


if __name__ == "__main__":
    unittest.main()
