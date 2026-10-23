# amber/summary.py
"""Print the results of a run from its CSV files (see amber.output).

    python3 -m amber.summary SIM_TIME_MS LOG_DIR [LOG_DIR ...]

A LOG_DIR holds the files of one run; reader_rx.csv and tag*_tx.csv are read. A Tag's packet is a
D2R carrying its data (kind msg2, cfa or data in tag<id>_tx.csv; Msg1 only asks for access and is
counted apart). A packet is decoded when the Reader decoded that Tag's data; collisions are
preambles the Reader found whose frame did not decode, which no Tag can be named for. Throughput
is decoded packets per simulated second.
"""

import csv
import glob
import os
import sys
from collections import Counter

DATA_KINDS = ("msg2", "cfa", "data")


def _rows(path):
    with open(path, newline="") as f:
        yield from csv.DictReader(f)


def summarize(log_dir: str, sim_time_ms: int) -> dict:
    sent, msg1 = Counter(), Counter()
    for path in sorted(glob.glob(os.path.join(log_dir, "tag*_tx.csv"))):
        node = os.path.basename(path)[len("tag"):-len("_tx.csv")]
        for r in _rows(path):
            (sent if r["kind"] in DATA_KINDS else msg1)[node] += 1
    if not sent and not msg1:
        raise ValueError(f"no transmissions found in {log_dir}")
    decoded, collided = Counter(), 0
    rx_path = os.path.join(log_dir, "reader_rx.csv")
    for r in _rows(rx_path) if os.path.exists(rx_path) else []:
        if r["kind"] == "data" and r["node_id"] != "-1":
            decoded[r["node_id"]] += 1
        elif r["kind"] == "collision":
            collided += 1
    nodes = sorted(set(sent) | set(msg1) | set(decoded), key=int)
    total_sent, total_dec = sum(sent.values()), sum(decoded.values())
    return {"sent": total_sent, "decoded": total_dec, "collided": collided, "msg1": sum(msg1.values()),
            "der": 1.0 - total_dec / total_sent if total_sent else 1.0,
            "throughput": total_dec / (sim_time_ms / 1000),
            "per_node": {n: (sent[n], decoded[n], msg1[n]) for n in nodes}}


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    sim_time_ms = int(argv[1])
    for log_dir in argv[2:]:
        s = summarize(log_dir, sim_time_ms)
        print(f"{log_dir}: sent={s['sent']} decoded={s['decoded']} collided={s['collided']} msg1={s['msg1']} "
              f"DER={s['der']:.2%} throughput={s['throughput']:.2f} pkt/s")
        for node, (sent, dec, m1) in s["per_node"].items():
            print(f"  node {node}: sent={sent} decoded={dec} msg1={m1}")


if __name__ == "__main__":
    main(sys.argv)
