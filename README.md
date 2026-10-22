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
python -m amber.run demo_experiments/broadcast/broadcast.toml         # 3 Tags, broadcast downlink
python -m amber.run demo_experiments/broadcast/broadcast_sic.toml     # the same with SIC
python -m amber.run demo_experiments/unicast/unicast.toml             # 3 Tags, asked one at a time
python -m amber.run demo_experiments/registration/registration.toml   # 4 Tags join, then send data
python -m amber.run demo_experiments/adaptive_aloha/adaptive_aloha.toml  # 12 Tags, adaptive framed-slotted ALOHA
python -m amber.run demo_experiments/gen2_q/gen2_q.toml               # 12 Tags, Gen2 Q algorithm
```

Each prints a summary and writes its CSV files to `output/<scenario>/` in the demo's folder:
`reader_bs<id>_rx.csv` (packets received) and `reader_bs<id>_tx.csv` (commands sent) for each base
station, `tag<id>_tx.csv` and `tag<id>_rx.csv` for each Tag, and the protocol's own files, such as
`adaptation.csv`. These are the same files, byte for byte, that the Reader and Tag containers
write on OAI ([amber-reader and amber-tag](https://github.com/nayreed/openairinterface5g/tree/aiot-rfsim/openair2/AMBER)),
which need a seed in the file. A file without a seed draws a new one for every run and prints it.
`python -m amber.run SCENARIO DIR` writes to `DIR` instead.

## Scenario files and protocols

A scenario can also be a TOML file: the values of a simulation (base stations, Tag groups,
radio, energy, duration, seed), with the protocol named by `[reader] protocol` and set by
`[reader.params]`. Each demo has one, such as
[`demo_experiments/broadcast/broadcast.toml`](demo_experiments/broadcast/broadcast.toml); the
keys are listed in `amber/scenario.py`. The protocols are in `amber/protocols/` (`broadcast`,
`unicast`, `registration`, `adaptive_aloha`, `gen2_q`). A new protocol goes there too, with a
demo scenario in `demo_experiments/<protocol>/`.
[`amber/protocols/README.md`](amber/protocols/README.md) explains each protocol, how to write a
new one, and the rules it must follow to run on OAI.

## Real waveforms

The Reader and the Tags always exchange real waveforms; there is no mode in which Amber decides
with formulas what is received, and nothing in a scenario file switches it on. The optional `[phy]`
table only sets the bit rates (`r2d_kbps`, `d2r_kbps`, `subcarrier_khz`); without it the defaults
of `amber.phy.PhyConfig` apply. The design follows 3GPP Rel-19
Ambient IoT for a monostatic Reader and backscatter Tags:

- The Reader's carrier is a signal (1.92 Msps), always on for the whole run. A command keys it
  off for part of each chip, so a Tag harvests less while one is sent. A Tag can only answer by
  reflecting the carrier.
- Commands are keyed onto the carrier (OOK, Manchester, CRC-16) as short bit fields, at
  `r2d_kbps`; a Tag decodes them with an envelope detector, or does not.
- A Tag answers by switching its reflection (BPSK backscatter on a square-wave subcarrier,
  Manchester, CRC-16) at `d2r_kbps`. Every Tag's reflection is added up, and the Reader
  demodulates the sum: two Tags at once really collide, and with `enable_sic` the Reader decodes
  the strongest, subtracts it and tries again.

`python -m amber.run` runs it on one machine; on OAI, rfsim carries the samples and adds the
Tags' signals up (normal rfsim), with the same files byte for byte. A scenario needs one base
station, integer payloads, and receive slots that follow each other with one length. A protocol
lists the commands it adds in `COMMANDS` and the bit width of their integer fields in
`COMMAND_FIELDS`. A run is slow: 12 Tags took about 4 s of run time per simulated second on one
core. `amber/phy.py` and `amber/iq.py` describe it in full.

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
| `phy`, `iq` | The waveforms, and the Reader and Tags exchanging them |
| `protocols` | The base station's protocol and the Tag class it needs, chosen by name |
| `scenario` | Scenarios as TOML files (`load`, `Scenario`) |
| `output` | The CSV files of a run, and the one function that writes them |
| `run` | `python -m amber.run`: run a scenario on one machine |
| `hosts`, `session`, `proxies` | The Reader and the Tags as separate hosts, one block of IQ samples every millisecond, as amber-reader and amber-tag run them on OAI |
| `summary` | Sent, decoded, collided, DER and throughput from a run's files |

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
