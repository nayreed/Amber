# tests/test_protocol_conformance.py
"""Checks every protocol in amber/protocols, found by listing the package, so a new protocol is
tested as soon as its file is there: its module loads, defines PROTOCOL as a Protocol subclass, its
default parameters pass check(), its policy is a generator and its outputs() has a header for
every row.

The Tag side of a protocol is fixed by 38.391 (amber.phy, amber.mac), so the old rules for Tag classes
(commands with a bit width, whole-millisecond slots, Tag randomness from self.rng) are gone. A
protocol is a Reader policy only, and the Reader draws nothing from the global random generator
either: the Reader container and a run on one machine must agree.
"""

import ast
import importlib
import inspect
import pkgutil
import unittest

import amber.protocols
from amber import protocols
from amber.protocols import Protocol

NOT_PROTOCOLS = {"base"}


def protocol_names():
    """The modules of amber/protocols that are protocols (all but base)."""
    return sorted(m.name for m in pkgutil.iter_modules(amber.protocols.__path__) if m.name not in NOT_PROTOCOLS)


def global_random_uses(source):
    """(line, text) of each use of the random module in source other than random.Random."""
    uses = []
    aliases = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(a.asname or a.name for a in node.names if a.name == "random")
        elif isinstance(node, ast.ImportFrom) and node.module == "random":
            uses += [(node.lineno, f"from random import {a.name}") for a in node.names if a.name != "Random"]
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases
                and node.attr != "Random"):
            uses.append((node.lineno, f"{node.value.id}.{node.attr}"))
    return sorted(uses)


class ConformanceTest(unittest.TestCase):
    def test_protocols_are_found(self):
        names = protocol_names()
        for name in ("broadcast", "unicast", "registration", "adaptive_aloha", "gen2_q"):
            self.assertIn(name, names)
        self.assertNotIn("base", names)

    def test_each_module_defines_a_protocol(self):
        for name in protocol_names():
            with self.subTest(name):
                cls = getattr(importlib.import_module(f"amber.protocols.{name}"), "PROTOCOL", None)
                self.assertTrue(isinstance(cls, type) and issubclass(cls, Protocol),
                                f"amber/protocols/{name}.py must define PROTOCOL as a Protocol subclass")

    def test_defaults_pass_check(self):
        for name in protocol_names():
            with self.subTest(name):
                protocol = protocols.load(name, {}, [0, 1, 2])
                self.assertEqual(protocol.params, protocol.PARAMS)
                self.assertEqual(protocol.name, name)
                self.assertEqual(protocol.node_ids, [0, 1, 2])

    def test_each_policy_is_a_generator_function(self):
        for name in protocol_names():
            with self.subTest(name):
                cls = importlib.import_module(f"amber.protocols.{name}").PROTOCOL
                self.assertTrue(inspect.isgeneratorfunction(cls.policy), f"{name}.policy must be a generator")

    def test_outputs_have_a_column_for_every_value(self):
        for name in protocol_names():
            with self.subTest(name):
                outputs = protocols.load(name, {}, [0]).outputs()
                for file, (columns, rows) in outputs.items():
                    self.assertTrue(file.endswith(".csv"))
                    self.assertTrue(all(len(row) == len(columns) for row in rows))

    def test_no_global_random(self):
        for name in protocol_names():
            with self.subTest(name):
                source = inspect.getsource(importlib.import_module(f"amber.protocols.{name}"))
                uses = global_random_uses(source)
                self.assertEqual(uses, [], f"amber/protocols/{name}.py uses the random module at {uses}: a "
                                           "run on one machine and the Reader container would differ")

    def test_source_check(self):
        self.assertEqual(global_random_uses("import random as r\nx = r.random()\ny = r.Random(3)\n"), [(2, "r.random")])
        self.assertEqual(global_random_uses("from random import Random, choice\n"), [(1, "from random import choice")])
        self.assertEqual(global_random_uses("import random\nrng = random.Random(1)\n"), [])


if __name__ == "__main__":
    unittest.main()
