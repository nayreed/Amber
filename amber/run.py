# amber/run.py
"""Run a scenario file in one process, print its results and write its CSV files.

    python -m amber.run demo_experiments/broadcast/broadcast_sic.toml [--out DIR]

The files go to output/<scenario name>/ next to the scenario file unless --out is given:
rx_packets.csv, tx_records.csv and the protocol's own files (such as adaptation.csv or q.csv).
A scenario without a seed draws a new one for every run, and the run prints it.
"""

import argparse
import os

from . import output, scenario
from .split.hosts import build_one_process
from .split.summary import summarize


def run(path: str, out_dir: str = None) -> str:
    """Run the scenario at path, write its files and print its results; returns the output directory."""
    s = scenario.load(path)
    out_dir = out_dir or os.path.join(os.path.dirname(os.path.abspath(path)), "output",
                                      os.path.splitext(os.path.basename(path))[0])
    env, bss, bsms = build_one_process(s)
    env.run(until=s.SIM_TIME_MS)
    os.makedirs(out_dir, exist_ok=True)
    output.write_packets(out_dir, bss, bsms)
    output.write_protocol_outputs(out_dir, s.protocol, bss)

    print(f"{path}: protocol {s.protocol.name}, {len(bsms)} Tags, {s.SIM_TIME_MS / 1000:g} s, seed {s.SEED}")
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
    parser.add_argument("--out", help="directory for the CSV files (default: output/<name>/ next to the file)")
    args = parser.parse_args(argv)
    run(args.scenario, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
