# tests/helpers.py
"""What several tests share: a scenario file cut to a short run, one run of a scenario as the
Reader and Tag sessions that amber-reader and amber-tag drive, and the records to compare."""

import contextlib
import os
import random
import re
import tempfile

from amber import iq

SCENARIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")
DEMO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "demo_experiments")
SEED = 12345


@contextlib.contextmanager
def shortened(path, sim_time_ms):
    """A copy of the scenario file at path, next to it so that its relative paths hold, that runs
    sim_time_ms instead; yields the copy's path and removes it afterwards."""
    with open(path) as f:
        text = f.read()
    text, n = re.subn(r"(?m)^sim_time_ms\s*=\s*\d+", f"sim_time_ms = {sim_time_ms}", text)
    assert n == 1, f"{path} has no sim_time_ms"
    with tempfile.NamedTemporaryFile("w", suffix=".toml", dir=os.path.dirname(os.path.abspath(path)), delete=False) as f:
        f.write(text)
    try:
        yield f.name
    finally:
        os.remove(f.name)


def scramble_global_random(node_id, ms):
    """Leave Python's global generator as an unrelated container might have it."""
    random.seed(f"other container {node_id} {ms}")


def run_sessions(scenario, out):
    """Run the scenario as separate containers would: the Tags answer in reverse order, each after
    Python's global generator has been left in an unrelated state, and the Reader reads the sum of
    their blocks. Returns the Reader's and the Tags' sessions, with their files written to out."""
    reader = iq.IqReaderSession(scenario, iq.BLOCK_BYTES, out)
    tags = []
    for node_id in reversed(scenario.NODE_IDS):
        scramble_global_random(node_id, -1)
        tags.append(iq.IqTagSession(scenario, node_id, iq.BLOCK_BYTES, out))
    done = False
    while not done:
        block = reader.next_block()
        answers, tags_done = [], []
        for tag in tags:
            scramble_global_random(tag.node_id, reader.host.env.now)
            answer, tag_done = tag.reply(block)
            answers.append(answer)
            tags_done.append(tag_done)
        done = reader.on_blocks([iq.add_blocks(answers)])
        assert tags_done == [done] * len(tags)
    reader.finish()
    for tag in tags:
        tag.finish()
    return reader, sorted(tags, key=lambda tag: tag.node_id)


def run_one_machine(scenario):
    """Run the scenario with iq.run(), as python -m amber.run does; returns the same as run_sessions()."""
    with tempfile.TemporaryDirectory() as out:
        return iq.run(scenario, out)


def records(reader, tags):
    """What a run did, for comparing two runs."""
    return ([(bs.rx_packets, bs.tx_packets) for bs in reader.host.bss],
            [(t.host.bsm.tx_records, t.host.bsm.rx_records, t.host.bsm.state) for t in tags])
