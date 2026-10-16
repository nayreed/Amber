[![docs](https://github.com/mrnmir/Amber/actions/workflows/docs.yml/badge.svg)](https://github.com/mrnmir/Amber/actions/workflows/docs.yml)
[![license](https://img.shields.io/badge/license-BSD--3--Clause-blue)](LICENSE)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](pyproject.toml)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo_white.svg">
  <source media="(prefers-color-scheme: light)" srcset="assets/logo_black.svg">
  <img src="assets/logo_black.svg" alt="AMBER Logo" width="120" align="left" hspace="20" vspace="6">
</picture>

### 6G Ambient IoT Simulator

A Python-based discrete-event simulator for 6G Ambient IoT Networks.

<br clear="left">

---

## About

AMBER simulates battery-free Ambient IoT devices that harvest energy from their
environment and communicate with base stations via backscatter. Built on
[SimPy](https://simpy.readthedocs.io/).

Developed at Uppsala University, Sweden.

## Installation

Requires Python 3.9+.

```bash
git clone https://github.com/mrnmir/Amber.git
cd Amber
pip install -e .
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

> **Note:** editable installs need pip 21.3 or newer. Upgrade with
> `python -m pip install --upgrade pip` if the install fails.

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

Each prints a summary and writes `rx_packets.csv`, `tx_records.csv` and the protocol's own files
(such as `adaptation.csv`) to `output/<scenario>/` in the demo's folder.

## Scenario files and protocols

A scenario can also be a TOML file: the values of a simulation (base stations, Tag groups,
radio, energy, duration, seed), with the protocol named by `[reader] protocol` and set by
`[reader.params]`. Each demo has one, such as
[`demo_experiments/broadcast/broadcast.toml`](demo_experiments/broadcast/broadcast.toml); the
keys are listed in `amber/scenario.py`. The protocols are in `amber/protocols/` (`broadcast`,
`unicast`, `registration`, `adaptive_aloha`, `gen2_q`). A new protocol goes there too, with a
demo scenario in `demo_experiments/<protocol>/`.
[`amber/protocols/README.md`](amber/protocols/README.md) explains each protocol, how to write a
new one, and the rules it must follow to run split on OAI.

To compare with a split run, write the logs a split run writes, then the CSV files from them:

```bash
python -m amber.split.reference demo_experiments/broadcast/broadcast.toml out/
python -m amber.split.export demo_experiments/broadcast/broadcast.toml out/
```

A file without a seed draws a new one for every run, and `amber.run` and `reference` print it. The same file runs split across processes with
[amber-reader and amber-tag](https://github.com/nayreed/openairinterface5g/tree/aiot-rfsim/openair2/AMBER),
which needs a seed in the file.

## Modules

| Module | Contents |
| --- | --- |
| `radiodevices` | `Node`, `Sector`, `BaseStation` topology dataclasses |
| `propagation` | Path loss, antenna patterns, `CoverageMap` |
| `capacitor` | Energy storage model (`Capacitor`, `CapacitorParams`) |
| `controller` | Voltage-threshold state machine driving node duty cycling |
| `energy` | `EnvEnergySource` — replays an external energy trace |
| `backscatter` | Node-side backscatter transmit/receive |
| `bsengine` | Base station behaviour, downlink scheduling |
| `packet_analysis` | Collision detection, SIC, throughput and DER statistics |
| `protocols` | The base station's protocol and the Tag class it needs, chosen by name |
| `scenario` | Scenarios as TOML files (`load`, `Scenario`) |
| `output` | The demos' CSV files from any run |
| `split` | One simulation run as a Reader and one process per Tag |

Full API reference, generated from the source docstrings, is at
**[mrnmir.github.io/Amber](https://mrnmir.github.io/Amber/)**.

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

## License

BSD 3-Clause — see [LICENSE](LICENSE).
