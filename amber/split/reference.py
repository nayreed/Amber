# amber/split/reference.py
"""Run a scenario in one process and write the logs a split run writes.

    python3 -m amber.split.reference SCENARIO OUTPUT_DIR

The files are those amber-reader and amber-tag write for the same scenario (see session.py), so
a split run can be checked with diff -r against this one.
"""

import os
import sys

from .hosts import build_one_process, load_scenario
from .session import write_reader_logs, write_tag_logs


def run(scenario_path: str, output_dir: str):
    scenario = load_scenario(scenario_path)
    env, bss, bsms = build_one_process(scenario)
    env.run(until=scenario.SIM_TIME_MS)
    os.makedirs(output_dir, exist_ok=True)
    write_reader_logs(output_dir, bss)
    for node_id, bsm in zip(scenario.NODE_IDS, bsms):
        write_tag_logs(output_dir, node_id, bsm)


def main(argv) -> int:
    if len(argv) != 3:
        print("usage: python3 -m amber.split.reference SCENARIO OUTPUT_DIR", file=sys.stderr)
        return 2
    run(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
