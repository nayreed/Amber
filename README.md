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

Amber's PHY (TS 38.291: the R2D and D2R waveforms and receivers) is OAI's C library
`libaiot_phy.so`, from `openair1/PHY/AIOT` of
[nayreed/openairinterface5g](https://github.com/nayreed/openairinterface5g), built on OAI's own CRC,
convolutional coder and Viterbi decoder. Build it once (needs git, cmake, a C compiler and SIMDe,
`libsimde-dev` on Ubuntu):

```bash
scripts/build_aiot_lib.sh                  # fetches the library's sources and builds amber/phy/lib/libaiot_phy.so
scripts/build_aiot_lib.sh /path/to/oai     # or builds from an OAI checkout
```

Or point `AMBER_AIOT_LIB` at a `libaiot_phy.so` you built elsewhere. OAI's `amber-reader` and
`amber-tag` set it to their own copy.

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
`reader_tx.csv` (the R2D messages sent) and `reader_rx.csv` (the D2Rs decoded, and the collisions)
for the Reader, `tag<id>_tx.csv` and `tag<id>_rx.csv` for each Tag, and the protocol's own files,
such as `adaptation.csv`. These are the same files, byte for byte, that the Reader and Tag containers
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

Every scenario has a `[phy]` table, and the Reader and the Tags exchange real waveforms; there is
no mode in which Amber decides with formulas what is received. The link is 3GPP Rel-19 Ambient IoT
(TS 38.291 and TS 38.391) for a monostatic Reader and backscatter Tags, at 5.76 Msps:

- The Reader's carrier is on for the whole run. The Reader sends 38.391 R2D messages on it as
  38.291 R2D frames, OFDM symbols of Manchester-coded chips; a Tag decodes them with an envelope
  detector, or does not.
- A Tag runs the 38.391 device MAC, the same for every protocol, and answers by switching its
  reflection of the carrier: a 38.291 D2R frame (convolutional code, repetition, preamble and
  midambles, OOK or BPSK on a small frequency shift) at T_R->D after the R2D it answers.
- Every Tag's reflection is added up, with the Reader's own carrier leak and its phase noise, and
  the Reader searches the sum for D2R frames: two Tags on one occasion and shift really collide,
  and with `enable_sic` the Reader subtracts what it decoded and searches again.

`python -m amber.run` runs it on one machine; on OAI, rfsim (or `rfsim_hub`) carries the samples
and adds the Tags' signals up, with the same files byte for byte. A scenario needs exactly one base
station. `amber/link.py`, `amber/phy` and `amber/mac` describe the link in full, and `amber/iq.py`
the blocks of samples the Reader and the Tags exchange.

## Network and topology

A scenario's optional `[network]` table says where the link runs, in the terms of the four
topologies of the Rel-19 A-IoT study: the 5G cell that shares the air (`cell`), who sends R2D
(`r2d`), who receives D2R (`d2r`), who sends the carrier the Tags reflect (`cw`), and whether a UE
forwards what it reads to the gNB (`relay`). Without it, a standalone Reader does all three, with
no 5G. `amber.scenario.Network` lists the values and maps them to topologies 1 to 4.
`python -m amber.run` simulates the A-IoT link only; OAI's `amber-compose` builds the room the
table describes, and says which combinations it cannot build yet. In topology 2 (`relay = true`)
the UE sends each message it hears from a Tag to the gNB, and `python -m amber.sink` takes them
there and writes `tag_data.csv`, in the columns of the UE's `reader_rx.csv`. In topology 3
(`r2d` and `d2r` at different nodes) the gNB runs the Reader's MAC and the UE sends its R2D or
receives its D2R, told what and when over its own user plane; Amber's Tags and CW node take part as
in the other rooms.

## Modules

| Module | Contents |
| --- | --- |
| `radiodevices` | `Node`, `Sector`, `BaseStation` topology dataclasses |
| `propagation` | Path loss, antenna patterns, `CoverageMap` |
| `capacitor` | Energy storage model (`Capacitor`, `CapacitorParams`) |
| `controller` | Voltage-threshold state machine driving node duty cycling |
| `energy` | `EnvEnergySource` — replays an external energy trace |
| `backscatter` | Node-side backscatter transmit/receive |
| `phy` | The Rel-19 A-IoT PHY (TS 38.291): its parameters, and OAI's C library that does the signal processing |
| `mac` | The Rel-19 A-IoT MAC (TS 38.391): every MAC PDU, and the device's and the Reader's procedures; Tag data, the message a Reader sends on what it heard |
| `link` | The Reader's and the Tags' link: 38.391 messages on real waveforms, one millisecond at a time |
| `iq` | The Reader and the Tags exchanging blocks of IQ samples, as rfsim carries them |
| `protocols` | The Reader policies (which 38.391 messages the Reader sends), chosen by name |
| `scenario` | Scenarios as TOML files (`load`, `Scenario`) |
| `output` | The CSV files of a run, and the one function that writes them |
| `run` | `python -m amber.run`: run a scenario on one machine |
| `hosts`, `session` | The Reader and the Tags as separate hosts, one block of IQ samples every millisecond, as amber-reader and amber-tag run them on OAI |
| `summary` | Sent, decoded, collided, DER and throughput from a run's files |
| `sink` | `python -m amber.sink`: the network's end of topology 2, the Tag data a UE sends written to `tag_data.csv` |

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
