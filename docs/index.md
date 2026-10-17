---
title: 6G Ambient IoT Simulator
---

# AMBER — 6G Ambient IoT Simulator

A Python-based discrete-event simulator for 6G Ambient IoT Networks.

AMBER simulates battery-free Ambient IoT devices that harvest energy from their
environment and communicate with base stations via backscatter. Built on
[SimPy](https://simpy.readthedocs.io/).

Developed at Uppsala University, Sweden.

## Installation

```bash
git clone https://github.com/mrnmir/Amber.git
cd Amber
pip install -e .
```

## Quickstart

Every protocol has a demo in `demo_experiments/<protocol>/`: one or more scenario files.
Run one with `amber.run`:

```bash
python -m amber.run demo_experiments/broadcast/broadcast.toml          # 3 Tags, broadcast downlink
python -m amber.run demo_experiments/broadcast/broadcast_sic.toml      # the same with SIC
python -m amber.run demo_experiments/unicast/unicast.toml              # 3 Tags, asked one at a time
python -m amber.run demo_experiments/registration/registration.toml    # 4 Tags join, then send data
python -m amber.run demo_experiments/adaptive_aloha/adaptive_aloha.toml  # 12 Tags, adaptive framed-slotted ALOHA
python -m amber.run demo_experiments/gen2_q/gen2_q.toml                # 12 Tags, Gen2 Q algorithm
```

Each prints a summary and writes its CSV files to `output/<scenario>/` in the demo's folder:
`reader_bs<id>_rx.csv` (packets received) and `reader_bs<id>_tx.csv` (commands sent) for each base
station, `tag<id>_tx.csv` and `tag<id>_rx.csv` for each Tag, and the protocol's own files, such as
`adaptation.csv`. These are the same files, byte for byte, that the Reader and Tag containers
write on OAI ([amber-reader and amber-tag](https://github.com/nayreed/openairinterface5g/tree/aiot-rfsim/openair2/AMBER)),
which need a seed in the file. A file without a seed draws a new one for every run and prints it.
`python -m amber.run SCENARIO DIR` writes to `DIR` instead.

## API Reference

The [API Reference](api/backscatter.md) is generated directly from the docstrings
in the `amber` package.

## Publication

A demo abstract describing AMBER:

> M. Manafova, T. Voigt, and L. Mottola. "Demo Abstract: Amber: 6G Ambient IoT
> Simulator." *2026 ACM/IEEE International Conference on Embedded Artificial
> Intelligence and Sensing Systems – Posters and Demos (SenSys-Adjunct)*,
> pp. 23–24, 2026.
> [doi:10.1109/SenSys-Adjunct71932.2026.00021](https://doi.org/10.1109/SenSys-Adjunct71932.2026.00021)

```bibtex
@INPROCEEDINGS{11574939,
  author={Manafova, Mirana and Voigt, Thiemo and Mottola, Luca},
  booktitle={2026 ACM/IEEE International Conference on Embedded Artificial Intelligence and Sensing Systems - Posters and Demos (SenSys-Adjunct)}, 
  title={Demo Abstract: Amber: 6G Ambient IoT Simulator}, 
  year={2026},
  volume={},
  number={},
  pages={23-24},
  keywords={Internet of Things;Modeling;Base stations;Backscatter;Energy harvesting;Energy;Printing;Ambient IoT;6G;IoT simulator;backscatter communication;energy harvesting},
  doi={10.1109/SenSys-Adjunct71932.2026.00021}}
```
