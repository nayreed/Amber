# amber/split/summary.py
"""Print the results Amber's demos print (sent, decoded, collided, DER, throughput, per node) from
the logs of a run.

    python3 -m amber.split.summary SIM_TIME_MS LOG_DIR [LOG_DIR ...]

A LOG_DIR is either the output of an amber.split run (reader_bs*_rx.csv, tag*_tx.csv) or a demo's
own output directory (rx_packets.csv, tx_records.csv). The numbers are computed as the demos compute
them: a packet is decoded when it did not collide, counted once per (node, start time), and
throughput is decoded packets per simulated second.
"""

import csv
import glob
import os
import sys
from collections import Counter


def _rows(paths):
    for path in paths:
        with open(path, newline="") as f:
            yield from csv.DictReader(f)


def summarize(log_dir: str, sim_time_ms: int) -> dict:
    if os.path.exists(os.path.join(log_dir, "rx_packets.csv")):
        rx = [(r["node_id"], r["time_ms"], r["collided"]) for r in _rows([os.path.join(log_dir, "rx_packets.csv")])]
        tx = [r["node_id"] for r in _rows([os.path.join(log_dir, "tx_records.csv")])]
    else:
        rx = [(r["node_id"], r["start_ms"], r["collided"]) for r in _rows(sorted(glob.glob(os.path.join(log_dir, "reader_bs*_rx.csv"))))]
        tx = []
        for path in sorted(glob.glob(os.path.join(log_dir, "tag*_tx.csv"))):
            node_id = os.path.basename(path)[len("tag"):-len("_tx.csv")]
            tx += [node_id for _ in _rows([path])]
    if not tx:
        raise ValueError(f"no transmissions found in {log_dir}")
    decoded = {(node, start) for node, start, collided in rx if collided == "False"}
    sent = Counter(tx)
    per_node = {node: (sent[node], sum(1 for n, _ in decoded if n == node),
                       sum(1 for n, _, c in rx if n == node and c == "True"))
                for node in sorted(set(sent) | {n for n, _, _ in rx}, key=int)}
    total_sent = sum(sent.values())
    return {"sent": total_sent, "decoded": len(decoded), "collided": sum(1 for *_, c in rx if c == "True"),
            "der": 1.0 - len(decoded) / total_sent, "throughput": len(decoded) / (sim_time_ms / 1000),
            "per_node": per_node}


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    sim_time_ms = int(argv[1])
    for log_dir in argv[2:]:
        s = summarize(log_dir, sim_time_ms)
        print(f"{log_dir}: sent={s['sent']} decoded={s['decoded']} collided={s['collided']} "
              f"DER={s['der']:.2%} throughput={s['throughput']:.2f} pkt/s")
        for node, (sent, dec, col) in s["per_node"].items():
            print(f"  node {node}: sent={sent} decoded={dec} collided={col}")


if __name__ == "__main__":
    main(sys.argv)
