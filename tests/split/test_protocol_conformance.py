# tests/split/test_protocol_conformance.py
"""Checks every protocol in amber/protocols, found by listing the package, so a new protocol is
tested as soon as its file is there.

Each one runs with its default parameters on the four-Tag registration scenario, once in one
process and once split into a Reader host and one Tag host per node, exchanging framed blocks as
amber-reader and amber-tag do; the records must be identical. The split run scrambles Python's
global random generator before every Tag step, so a Tag that draws from it instead of self.rng
fails, and it packs every command and transmission as JSON in 8192-byte blocks at whole
milliseconds, so a protocol that sends something else fails too. A protocol whose largest message
uses more than half a block on this small scenario fails as well, since a larger layout would
overflow it.

Each protocol module is also read for uses of the random module other than random.Random, which
gives a clearer message than a mismatch.
"""

import ast
import importlib
import inspect
import os
import pkgutil
import sys
import tempfile
import textwrap
import unittest

import amber.protocols
from amber import scenario
from amber.protocols import Protocol

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_split_equivalence import BLOCK_BYTES, run_one_process, run_split  # noqa: E402

SCENARIO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios", "registration.toml")
NOT_PROTOCOLS = {"base"}


def protocol_names():
    """The modules of amber/protocols that are protocols (all but base)."""
    return sorted(m.name for m in pkgutil.iter_modules(amber.protocols.__path__) if m.name not in NOT_PROTOCOLS)


def loader(protocol):
    """Loads the scenario running protocol, given by name, with its default parameters."""
    return lambda path: scenario.load(path, {"reader.protocol": protocol, "reader.params": {}})


def global_random_uses(source):
    """(line, text) of each use of the random module in source other than random.Random."""
    uses = []
    aliases = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            aliases.update(a.asname or a.name for a in node.names if a.name == "random")
        elif isinstance(node, ast.ImportFrom) and node.module == "random":
            uses += [(node.lineno, f"from random import {a.name}") for a in node.names if a.name != "Random"]
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases
                and node.attr != "Random"):
            uses.append((node.lineno, f"{node.value.id}.{node.attr}"))
    return sorted(uses)


def records(bss, bsms):
    return ([(bs.rx_packets, bs.tx_packets) for bs in bss],
            [(bsm.tx_records, bsm.rx_records, bsm.state) for bsm in bsms])


class ConformanceTest(unittest.TestCase):
    def test_protocols_are_found(self):
        names = protocol_names()
        self.assertIn("broadcast", names)
        self.assertIn("gen2_q", names)
        self.assertNotIn("base", names)

    def test_each_module_defines_a_protocol(self):
        for name in protocol_names():
            with self.subTest(name):
                cls = getattr(importlib.import_module(f"amber.protocols.{name}"), "PROTOCOL", None)
                self.assertTrue(isinstance(cls, type) and issubclass(cls, Protocol),
                                f"amber/protocols/{name}.py must define PROTOCOL as a Protocol subclass")

    def test_no_global_random(self):
        for name in protocol_names():
            with self.subTest(name):
                source = inspect.getsource(importlib.import_module(f"amber.protocols.{name}"))
                uses = global_random_uses(source)
                self.assertEqual(uses, [], f"amber/protocols/{name}.py uses the random module at {uses}: a Tag "
                                           "must draw from self.rng, its own generator, or a split run differs")

    def test_split_equals_one_process(self):
        for name in protocol_names():
            with self.subTest(name):
                one = records(*run_one_process(SCENARIO, loader(name)))
                self.assertGreater(sum(len(rx) for rx, _ in one[0]), 0,
                                   f"{name} received nothing on the scenario, so the check proves nothing")
                sizes = {}
                self.assertEqual(one, records(*run_split(SCENARIO, loader(name), sizes)),
                                 f"{name}: a split run differs from one process")
                for side, size in sizes.items():
                    self.assertLessEqual(size, BLOCK_BYTES // 2,
                                         f"{name}: the {side}'s largest message is {size} bytes, more than half "
                                         f"a {BLOCK_BYTES}-byte block on a four-Tag scenario")


BAD_PROTOCOL = '''
import random

from amber import backscatter
from amber.protocols.broadcast import Broadcast


class Node(backscatter.BackscatterModule):
    def handle_command(self, cmd, bs_id, data):
        if cmd == "send_data":
            self.chosen_slot_idx = random.randint(0, len(self.rx_slots) - 1) if self.rx_slots else -1
            self.last_tx_command_time = self.env.now


class Bad(Broadcast):
    TAG_CLASS = Node


PROTOCOL = Bad
'''


class CatchesMistakesTest(unittest.TestCase):
    """The checks fail a protocol whose Tags draw from the random module."""

    def test_source_check(self):
        self.assertEqual(global_random_uses(BAD_PROTOCOL), [(11, "random.randint")])
        self.assertEqual(global_random_uses("import random as r\nx = r.random()\ny = r.Random(3)\n"), [(2, "r.random")])
        self.assertEqual(global_random_uses("from random import Random, choice\n"), [(1, "from random import choice")])
        self.assertEqual(global_random_uses("import random\nrng = random.Random(1)\n"), [])

    def test_split_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "bad_protocols"))
            open(os.path.join(tmp, "bad_protocols", "__init__.py"), "w").close()
            with open(os.path.join(tmp, "bad_protocols", "bad.py"), "w") as f:
                f.write(textwrap.dedent(BAD_PROTOCOL))
            sys.path.insert(0, tmp)
            try:
                load = loader("bad_protocols.bad")
                one = records(*run_one_process(SCENARIO, load))
                self.assertGreater(sum(len(rx) for rx, _ in one[0]), 0)
                self.assertNotEqual(one, records(*run_split(SCENARIO, load)))
            finally:
                sys.path.remove(tmp)
                for name in [m for m in sys.modules if m.startswith("bad_protocols")]:
                    del sys.modules[name]


if __name__ == "__main__":
    unittest.main()
