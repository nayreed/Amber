# amber/run.py
"""Run a scenario on one machine, write its CSV files and print its results.

    python -m amber.run SCENARIO [OUTPUT_DIR]

The files are those the Reader and Tag containers on OAI write together for the same scenario and
seed (see amber.output), byte for byte. Without OUTPUT_DIR they go to output/<scenario name>/ next
to the scenario file. A scenario without a seed draws a new one for every run, and the run prints it.
"""

import argparse
import os

from . import output
from .hosts import build, load_scenario
from .summary import summarize


def default_output_dir(path: str) -> str:
    """output/<scenario name>/ next to the scenario file."""
    return os.path.join(os.path.dirname(os.path.abspath(path)), "output", os.path.splitext(os.path.basename(path))[0])


def run(path: str, out_dir: str = None) -> str:
    """Run the scenario at path, write its files and print its results; returns the output directory."""
    s = load_scenario(path)
    out_dir = out_dir or default_output_dir(path)
    env, bss, bsms = build(s)
    env.run(until=s.SIM_TIME_MS)
    output.write_run(out_dir, s, bss, bsms)

    name = s.protocol.name if hasattr(s, "protocol") else "-"
    print(f"{path}: protocol {name}, {len(bsms)} Tags, {s.SIM_TIME_MS / 1000:g} s, seed {getattr(s, 'SEED', '-')}")
    try:
        r = summarize(out_dir, s.SIM_TIME_MS)
    except ValueError:
        print("  no Tag transmitted")
    else:
        print(f"  sent={r['sent']} decoded={r['decoded']} collided={r['collided']} "
              f"DER={r['der']:.2%} throughput={r['throughput']:.2f} pkt/s")
        for node, (sent, dec, col) in r["per_node"].items():
            print(f"  node {node}: sent={sent} decoded={dec} collided={col}")
    print(f"  files in {out_dir}")
    return out_dir


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", help="a scenario TOML file")
    parser.add_argument("output_dir", nargs="?", help="directory for the CSV files (default: output/<name>/ next to the file)")
    args = parser.parse_args(argv)
    run(args.scenario, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
