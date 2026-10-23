# tests/test_protocols.py
"""Checks amber.protocols: each protocol checks its parameters, a scenario can name a protocol of its
own by module path, and a short run of each protocol's scenario in tests/scenarios decodes Tag data
(and, for gen2_q and adaptive_aloha, writes the file the protocol adds)."""

import csv
import importlib
import os
import sys
import tempfile
import textwrap
import unittest

from amber import iq, protocols, scenario

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import LONG_MS, SCENARIO_DIR, SHORT_MS  # noqa: E402


class ParamsTest(unittest.TestCase):
    def test_defaults_and_values(self):
        protocol = protocols.load("broadcast", {"occasions_exp": 4}, [0])
        self.assertEqual(protocol.params, {"occasions_exp": 4})
        self.assertEqual(protocol.name, "broadcast")
        self.assertEqual(protocols.load("gen2_q", {}, [0]).params, {"q0": 4.0, "c": 0.3})
        self.assertEqual(protocols.load("unicast", {}, [0, 1]).node_ids, [0, 1])

    def test_unknown_parameter(self):
        with self.assertRaisesRegex(ValueError, "broadcast has no parameter occasions "):
            protocols.load("broadcast", {"occasions": 4}, [0])
        with self.assertRaisesRegex(ValueError, "unicast has no parameter slots "):
            protocols.load("unicast", {"slots": 4}, [0])

    def test_wrong_kind(self):
        for value in (2.0, True, "2"):
            with self.assertRaisesRegex(ValueError, "occasions_exp must be an integer"):
                protocols.load("broadcast", {"occasions_exp": value}, [0])
        # A float parameter takes an integer too.
        self.assertEqual(protocols.load("adaptive_aloha", {"smoothing": 1}, [0]).params["smoothing"], 1)
        self.assertEqual(protocols.load("gen2_q", {"q0": 3}, [0]).params["q0"], 3)

    def test_out_of_range(self):
        with self.assertRaisesRegex(ValueError, "occasions_exp must be within"):
            protocols.load("broadcast", {"occasions_exp": 16}, [0])
        with self.assertRaisesRegex(ValueError, "occasions_exp must be within"):
            protocols.load("registration", {"occasions_exp": -1}, [0])
        with self.assertRaisesRegex(ValueError, "start_exp must be within"):
            protocols.load("adaptive_aloha", {"start_exp": 8}, [0])
        with self.assertRaisesRegex(ValueError, "max_exp must be within"):
            protocols.load("adaptive_aloha", {"min_exp": 3, "max_exp": 2, "start_exp": 3}, [0])
        with self.assertRaisesRegex(ValueError, "occasions_per_contender must be at least 1"):
            protocols.load("adaptive_aloha", {"occasions_per_contender": 0}, [0])
        with self.assertRaisesRegex(ValueError, "smoothing must be in"):
            protocols.load("adaptive_aloha", {"smoothing": 0.0}, [0])
        with self.assertRaisesRegex(ValueError, "q0 must be within"):
            protocols.load("gen2_q", {"q0": 16.0}, [0])
        with self.assertRaisesRegex(ValueError, "c must be in"):
            protocols.load("gen2_q", {"c": 0.0}, [0])

    def test_within_and_positive_name_the_parameter(self):
        protocol = protocols.load("broadcast", {}, [0])
        protocol.within("occasions_exp", 0, 15)
        with self.assertRaisesRegex(ValueError, "protocol broadcast: occasions_exp must be within \\[3, 4\\]"):
            protocol.within("occasions_exp", 3, 4)

    def test_occasions_code_and_the_pagings_to_play_them_out(self):
        for n, v in ((1, 0), (2, 1), (3, 2), (4, 2), (5, 3)):
            self.assertEqual(protocols.Protocol.occasions_code(n), v)
        with self.assertRaises(ValueError):
            protocols.Protocol.occasions_code(2 ** 16)


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
                PARAMS = {"occasions_exp": 1}

                def check(self):
                    self.within("occasions_exp", 0, 3)

                def policy(self, ctx):
                    while True:
                        _, h2 = yield from self.access(ctx, ctx.mac.paging_cbra(
                            ctx.next_tid(), ctx.cfg.cbra(), n_occasions_code=self.params["occasions_exp"]))

            PROTOCOL = Poll
        """)
        protocol = protocols.load("my_protocols.poll", {"occasions_exp": 2}, [0])
        self.assertEqual((protocol.name, protocol.params), ("my_protocols.poll", {"occasions_exp": 2}))
        with self.assertRaisesRegex(ValueError, "occasions_exp must be within"):
            protocols.load("my_protocols.poll", {"occasions_exp": 9}, [0])

    def test_own_protocol_runs_in_a_scenario(self):
        self.write("poll", """
            from amber.protocols import Protocol

            class Poll(Protocol):
                def policy(self, ctx):
                    while True:
                        yield from self.access(ctx, ctx.mac.paging_cbra(ctx.next_tid(), ctx.cfg.cbra(), n_occasions_code=0))

            PROTOCOL = Poll
        """)
        s = scenario.load(os.path.join(SCENARIO_DIR, "single_node.toml"),
                          {"reader.protocol": "my_protocols.poll", "reader.params": {}, "run.sim_time_ms": SHORT_MS})
        with tempfile.TemporaryDirectory() as out:
            reader, _ = iq.run(s, out)
        self.assertGreater(len([r for r in reader.record.rx if r[2] == "data"]), 0)

    def test_module_without_protocol(self):
        self.write("empty", "X = 1\n")
        with self.assertRaisesRegex(ValueError, "does not define PROTOCOL"):
            protocols.load("my_protocols.empty", {}, [0])

    def test_missing_import_inside_a_protocol_is_not_hidden(self):
        self.write("broken", "import module_that_does_not_exist\n")
        with self.assertRaises(ModuleNotFoundError):
            protocols.load("my_protocols.broken", {}, [0])


def rows(path):
    with open(path, newline="") as f:
        return list(csv.reader(f))


class RunTest(unittest.TestCase):
    """A short run of each protocol's scenario decodes Tag data."""

    def run_protocol(self, name, sim_time_ms):
        s = scenario.load(os.path.join(SCENARIO_DIR, name + ".toml"), {"run.sim_time_ms": sim_time_ms})
        with tempfile.TemporaryDirectory() as out:
            reader, tags = iq.run(s, out)
            files = {f: rows(os.path.join(out, f)) for f in os.listdir(out) if f.endswith(".csv")}
        data = [r for r in reader.record.rx if r[2] == "data"]
        self.assertGreater(len(data), 0, f"{name} decoded no Tag data in {sim_time_ms} ms")
        self.assertTrue(all(r[3] in s.NODE_IDS for r in data))
        return reader, data, files

    def test_broadcast(self):
        reader, data, _ = self.run_protocol("broadcast", LONG_MS)
        self.assertEqual(reader.protocol.name, "broadcast")
        self.assertIn("paging_cbra", {r[2] for r in reader.record.tx})

    def test_unicast(self):
        reader, data, files = self.run_protocol("unicast", LONG_MS)
        # Tags are asked one by one, by their Paging ID
        self.assertIn("paging_cfa", {r[2] for r in reader.record.tx})
        self.assertNotIn("q.csv", files)

    def test_registration(self):
        reader, data, _ = self.run_protocol("registration", SHORT_MS)
        # A joined Tag is asked by its AS ID; it answers with Upper Layer Data
        self.assertIn("random_id_response", {r[1] for r in reader.record.rx})

    def test_adaptive_aloha_writes_adaptation_csv(self):
        _, _, files = self.run_protocol("adaptive_aloha", SHORT_MS)
        adaptation = files["adaptation.csv"]
        self.assertEqual(adaptation[0], ["time_s", "n_occasions", "heard", "collided"])
        self.assertGreater(len(adaptation), 1)
        self.assertTrue(all(int(r[1]) in (2, 4, 8, 16, 32, 64) for r in adaptation[1:]))
        self.assertNotIn("q.csv", files)

    def test_gen2_q_writes_q_csv(self):
        _, _, files = self.run_protocol("gen2_q", SHORT_MS)
        q = files["q.csv"]
        self.assertEqual(q[0], ["time_s", "q", "decoded", "collided"])
        self.assertGreater(len(q), 1)
        self.assertTrue(all(0 <= int(r[1]) <= 15 for r in q[1:]))
        self.assertNotIn("adaptation.csv", files)


if __name__ == "__main__":
    unittest.main()
