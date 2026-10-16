# Protocols

A protocol decides two things:
- what the base station (the Reader) sends, and when it listens;
- how each Tag reacts to what it hears.

A scenario file picks a protocol by name and sets its parameters:

```toml
[reader]
protocol = "gen2_q"

[reader.params]
q0 = 4.0
c = 0.3
```

Any parameter you leave out keeps its default. A parameter the protocol doesn't have, or a value of the wrong type, is refused when the scenario loads.

This folder holds every protocol. **A new protocol goes here too**, as one Python file.

- [How a protocol runs](#how-a-protocol-runs)
- [The protocols in this folder](#the-protocols-in-this-folder)
- [Writing a new protocol](#writing-a-new-protocol)
- [Rules for running split (OAI)](#rules-for-running-split-oai)
- [Pitfalls](#pitfalls)
- [Testing](#testing)

---

## How a protocol runs

### The Reader side

The base station plays **frames**. A frame is a list of entries:

```python
("tx", duration_ms, slot_id, payload)   # send a command, e.g. {"cmd": "send_data", "target": -1}
("rx", duration_ms, slot_id)            # listen for one slot
```

There are two ways to give the frames.

- **A static schedule.** It is one fixed frame, repeated forever (`loop=True`). Broadcast, unicast and registration work this way.
- **A policy.** It is a generator that yields one frame at a time. It resumes after the frame has finished, so it can look at what happened and choose the next frame. Adaptive ALOHA and gen2_q work this way. Between frames, a policy can read these on the base station (`bs`):
  - `bs.decoded_this_frame`: the ids of the Tags decoded in this frame.
  - `bs.slot_results`: one entry per listening slot of this frame, empty slots included. Each entry has the ids decoded in that slot, the number of packets that collided, and the shortcuts `empty`, `success` and `collision`. A slot can hold a decoded packet and a collided one at the same time, and `collision` counts that slot as a collision.
  - `bs.total_collided`: collided packets since the start of the run (it never resets).

A command's `target` says who receives it:
- `-1` sends it to every Tag;
- a node id sends it to that one Tag;
- a list of ids sends it to those Tags.

The command `"ack"` is special. It acknowledges every Tag whose packet was decoded since the last ack and wasn't registered yet, and those Tags become registered.

### Which slots a command offers

Every command carries `rx_slots`: the listening slots that follow it, up to the next `tx` entry, as `(start_ms, end_ms)` pairs. A looping schedule that has no `tx` entry left continues into the next frame. A Tag can only answer in one of the slots its latest command offered.

- A command sent to one Tag, or to a list of Tags, offers **only the first slot**.
- A new command replaces the Tag's slots, so put a command and the slots it should offer in the same frame.

### The Tag side

Every Tag is a `BackscatterModule` (or the protocol's subclass of it), driven by a `Controller` that models its energy. The parts a protocol touches are:

| Where | What it does |
|---|---|
| `handle_command(cmd, bs_id, data)` | Called for every command the Tag hears. `self.rx_slots` already holds the offered slots. To answer, set `self.chosen_slot_idx` (an index into `rx_slots`, or `-1` for no answer) and `self.last_tx_command_time = self.env.now`. |
| `build_tx_payload(sensor_data)` | Called when the Tag is inside its chosen slot and has the energy to send. Return `{"type": ..., "payload": ...}`, or `None` to stay silent. |
| `self.state` | The Tag's protocol state. The built-in protocol uses `"idle"`, `"wait_ack"` and `"registered"`. |
| `self.rng` | The Tag's own random generator. |

**A Tag hears commands only while it is on.** While its capacitor is charging (at the start, or after it dropped below the low threshold) the controller is off and `receive_command` ignores everything, so the Tag keeps the slots and state it had.

A Tag only transmits when all of these hold:
1. its capacitor has enough charge, so the controller is on;
2. it got a command, and `chosen_slot_idx` points to one of that command's slots;
3. the current time is inside that slot.

After it transmits, its slot is cleared, so it needs a new command before it sends again.

**The controller only senses data when `self.state == "registered"`.** In any other state the Tag has nothing to send, and `sensor_data` is 0. See [Pitfalls](#pitfalls).

---

## The protocols in this folder

### `broadcast`

**What it is.** The simplest polling scheme: ask everyone, then listen.

**One frame:**
1. one `send_data` command to all Tags, lasting `tx_ms`;
2. then `slots` listening slots of `rx_ms` each.

Every Tag that heard the command and has the energy answers with data in a slot it picks at random. Two Tags that pick the same slot collide. With SIC on, the stronger one may still be decoded.

**Setup.** Every Tag starts as `registered`, and the base station lists them all as registered, so there is no join phase.

**Parameters:**

| Parameter | Default | Meaning |
|---|---|---|
| `slots` | 2 | listening slots per frame |
| `tx_ms` | 5 | command length |
| `rx_ms` | 10 | slot length |

**Use it for:** a few Tags, or testing energy and radio settings. With more Tags than slots it collides a lot, because the number of slots never changes. Its demos are `demo_experiments/broadcast/broadcast.toml` and `broadcast_sic.toml`.

### `unicast`

**What it is.** Round-robin polling, one Tag at a time.

**One frame**, for each Tag in the scenario's order:
1. a `send_data` command addressed to that Tag (`tx_ms`);
2. one listening slot (`rx_ms`).

Only the addressed Tag answers, so there are no collisions between Tags.

**Setup.** As with broadcast, all Tags start registered.

**Parameters:**

| Parameter | Default | Meaning |
|---|---|---|
| `tx_ms` | 5 | command length |
| `rx_ms` | 10 | slot length |

**Use it for:** a collision-free baseline. A frame lasts `(tx_ms + rx_ms)` × the number of Tags, so with many Tags each one is asked rarely. A Tag that has no energy when its turn comes simply misses that turn. Its demo is `demo_experiments/unicast/`.

### `registration`

**What it is.** Amber's built-in protocol from the start: Tags join first, then send data. It uses the default `BackscatterModule` behaviour, with no Tag subclass. Its demo is `demo_experiments/registration/`.

**One frame:**
1. **Join.** A `send_id` command to all Tags (`tx_ms`), then `id_slots` listening slots (`rx_ms`). Every Tag that isn't registered yet answers with its id in a random slot, and moves to `wait_ack`.
2. **Ack.** An `ack` command (`tx_ms`) to every Tag whose id was decoded. Those Tags become `registered`.
3. **Data.** A `send_data` command to all Tags (`tx_ms`), then `data_slots` listening slots (`rx_ms`). The registered Tags answer with data in a random slot.

A registered Tag ignores later `send_id` commands. A Tag whose id collided stays in `wait_ack` and tries again in the next frame.

**Parameters:**

| Parameter | Default | Meaning |
|---|---|---|
| `id_slots` | 3 | slots for joining |
| `data_slots` | 2 | slots for data |
| `tx_ms` | 5 | length of each of the three commands |
| `rx_ms` | 10 | slot length |

**Use it for:** studying how long Tags take to join, or anything that needs a join step. The slot counts are fixed, so many Tags joining at once collide a lot.

### `adaptive_aloha`

**What it is.** Framed slotted ALOHA whose frame size follows the load. It decides once per frame. Its demo is `demo_experiments/adaptive_aloha/` (formerly `custom_protocol`).

**One frame:**
1. a `collect` command to all Tags (`tx_ms`);
2. then `n` listening slots (`slot_ms`).

Each Tag that hears it answers in a random slot.

**How `n` adapts.** After each frame, the policy:
1. counts the Tags it heard and the packets that collided;
2. smooths that load: `load = (1 - smoothing) × load + smoothing × (heard + collided)`;
3. moves `n` one step towards `slots_per_contender × load`, staying within `[min_slots, max_slots]`.

A one-slot dead band above the target stops `n` from flipping back and forth. `n` starts at `start_slots`.

**Tags.** `AdaptiveAlohaNode` answers `collect` in a random slot and becomes `registered` on its first `collect`, so its controller senses data and every answer carries a sensed value.

**Parameters:**

| Parameter | Default | Meaning |
|---|---|---|
| `start_slots` | 20 | `n` in the first frame |
| `min_slots`, `max_slots` | 2, 32 | the range for `n` |
| `tx_ms` | 5 | command length |
| `slot_ms` | 8 | slot length |
| `smoothing` | 0.25 | weight of the newest frame in the load, in (0, 1] |
| `slots_per_contender` | 2 | slots per estimated contending Tag |

**Output.** `adaptation.csv` (one file per base station if there are several), with `time_s, n_slots, heard, collided` for every frame.

**Use it for:** many Tags whose number is unknown or changes. The load counts collided *packets*, not collided slots, and it can't see empty slots. A protocol that reads `bs.slot_results` can estimate the load better.

### `gen2_q`

**What it is.** The EPC Gen2 Q algorithm. It decides after **every slot**. Its demo is `demo_experiments/gen2_q/`.

**Rounds:**
- A round starts with a `query` command carrying `q`. Each Tag loads a counter, drawn at random from 0 to 2^q − 1.
- Each later frame is a `queryrep` command, and every Tag counts its counter down by one.
- The Tag whose counter is 0 answers in the slot right after the command.
- A round has 2^q frames, and each frame is exactly one command (`tx_ms`) and one slot (`slot_ms`).

**How Q adapts.** After every slot, the policy reads `bs.slot_results` and updates a running value `Qf` (it starts at `q0`):
- after a collision, it adds `c`;
- after an empty slot, it subtracts `c`;
- after a success, it leaves `Qf` alone.

`Qf` stays within [0, 15], and the next round uses `q = round(Qf)`. A slot with both a decoded and a collided packet counts as a collision, as in Gen2.

**Differences from real Gen2.** There is no RN16 / ACK / EPC handshake. Each Tag answers with its data once per round, and every round asks every Tag again. Tags start `registered`, so they send real sensor data.

**Parameters:**

| Parameter | Default | Meaning |
|---|---|---|
| `q0` | 4.0 | starting Qf, in [0, 15] |
| `c` | 0.3 | step size, in (0, 1] |
| `tx_ms` | 1 | command length |
| `slot_ms` | 10 | slot length |

**Output.** `q.csv` (one file per base station if there are several), with `time_s, q, decoded, collided` for every slot.

**Use it for:** an algorithm that reacts within a frame, or as the example of a protocol that decides after every slot. The cost is a command in front of every slot, as with Gen2's QueryRep.

### Choosing

| You want | Use |
|---|---|
| A few Tags, simplest possible | `broadcast` |
| No collisions, Tags asked in turn | `unicast` |
| Tags must join before sending data | `registration` |
| Many Tags, frame size adapts, decided per frame | `adaptive_aloha` |
| Many Tags, decided after every slot | `gen2_q` |

---

## Writing a new protocol

Add one file, for example `amber/protocols/tdma.py`. It needs:

1. **A `Protocol` subclass** (from `.base`) with:
   - `PARAMS`: parameter names and their defaults. The default's type is the type a value must have: write `5` for an integer and `0.5` for a number.
   - `reader_args()`: return either `{"schedule": [...], "loop": True}` or `{"policy": self._policy}`.
   - Optionally:
     - `check()` to validate values (`self.positive("tx_ms", ...)` requires at least 1);
     - `TAG_CLASS` for your own Tag class;
     - `setup_tag(bsm)` and `setup_reader(bs)` to prepare each Tag and base station before the run;
     - `outputs(bss)` for extra CSV files.
2. **A Tag class**, only if Tags react differently from the built-in protocol. Subclass `backscatter.BackscatterModule` and override `handle_command` and, if needed, `build_tx_payload`.
3. **`PROTOCOL = YourClass`** at the end of the file.

Then add its demo: a folder `demo_experiments/tdma/` holding a scenario `tdma.toml` that uses `protocol = "tdma"` (copy another demo's file and change `[reader]`). `python -m amber.run demo_experiments/tdma/tdma.toml` runs it and writes its results to `demo_experiments/tdma/output/tdma/`.

### Example: TDMA

One `sync` command, then one slot per Tag. Each Tag owns the slot at its position in the scenario's Tag list, so Tags never collide.

```python
# amber/protocols/tdma.py
from .. import backscatter
from .base import Protocol


class TdmaTag(backscatter.BackscatterModule):
    my_slot = -1  # set by Tdma.setup_tag

    def handle_command(self, cmd, bs_id, data):
        if cmd == "sync":
            self.chosen_slot_idx = self.my_slot if self.my_slot < len(self.rx_slots) else -1
            self.last_tx_command_time = self.env.now

    def build_tx_payload(self, sensor_data):
        return {"type": "data", "payload": sensor_data}


class Tdma(Protocol):
    PARAMS = {"tx_ms": 5, "slot_ms": 10}
    TAG_CLASS = TdmaTag

    def check(self):
        self.positive("tx_ms", "slot_ms")

    def reader_args(self):
        p = self.params
        schedule = [("tx", p["tx_ms"], "sync", {"cmd": "sync", "target": -1})]
        schedule += [("rx", p["slot_ms"], "slot") for _ in self.node_ids]
        return {"schedule": schedule, "loop": True}

    def setup_tag(self, bsm):
        bsm.state = "registered"  # so the controller senses data
        bsm.my_slot = self.node_ids.index(bsm.node.id)


PROTOCOL = Tdma
```

For a policy, look at `adaptive_aloha.py` (it decides once per frame) and `gen2_q.py` (it decides after every slot).

---

## Rules for running split (OAI)

OAI's `amber-reader` and `amber-tag` run the Reader and each Tag in **separate processes**. They exchange one message each way every simulated millisecond, as JSON. A protocol must give the same results there as in one process, so it must follow three rules.

1. **Tag randomness comes from `self.rng`**, never from the `random` module. The `random` module's numbers depend on what else ran in the same process, so a split run would differ. The same goes for numpy's global generator. The policy runs on the Reader and may use its own `random.Random(...)`.

2. **Every duration is a whole number of milliseconds.** The split advances one millisecond at a time, and Tags only act on their 1 ms tick. A duration like `10.0` that comes out of float maths is fine; `10.5` isn't. Amber refuses such a frame with an error, in one process too.

3. **Commands and Tag payloads are plain JSON values:** dicts with string keys, lists, strings, ints, floats, bools and `None`. Anything else either fails or arrives changed:

   | You send | The other side gets |
   |---|---|
   | tuple `(1, 2)` | list `[1, 2]` |
   | dict `{1: "a"}` | `{"1": "a"}`, so `d[1]` raises KeyError |
   | set, bytes, numpy `int64` | an error |

   The changed values are the dangerous ones, because nothing fails: the split run just gives different results. The tuples Amber itself puts in `rx_slots` are fine, because Amber restores them.

**Message size.** All of one millisecond's commands travel in one block of 8192 bytes (`block_bytes` in OAI's `amber` section).
- A command to all Tags is encoded once, plus about 4 bytes per Tag it reaches, so about 1 KB for 250 Tags.
- Each *different* command costs about 85 bytes, so a block holds about 95 different commands in the same millisecond.

The protocols here use at most 1.3 KB with 250 Tags. A protocol that sends every Tag its own command in the same millisecond will run out sooner. Raising `block_bytes` works, but every block is sent at full size to every Tag every millisecond, so it slows the run.

---

## Pitfalls

- **Use the state `"registered"` if your Tags should send real data.** The controller only senses when `bsm.state == "registered"`. In any other state `sensor_data` is 0. Set the state in `setup_tag` (as broadcast and gen2_q do) or in `handle_command` (as adaptive_aloha does).
- **A Tag that was off missed what was sent meanwhile.** It wakes up with the slots and state it had before. A protocol whose Tags count something per command (like gen2_q's slot counter) should expect Tags whose count is out of step, as with real Tags. Real Gen2 Tags also forget such state when power drops; Amber's Tags don't, unless the protocol does it.
- **Set `last_tx_command_time`** when the Tag should answer. Without it the Tag never transmits, and a slot that started before that time is treated as stale.
- **A command sent to specific Tags offers only one slot.** Pick index 0.
- **The `random` module looks fine in one process.** It only goes wrong when split. The conformance test catches it.
- **`total_collided` never resets.** Keep the previous value and subtract, as `adaptive_aloha` does, or use `bs.slot_results`.

---

## Testing

From the repository root:

```bash
# Every protocol here, in one process and split (finds new files automatically)
PYTHONPATH=. python3 -m unittest tests/split/test_protocol_conformance.py

# Everything
PYTHONPATH=. python3 -m unittest discover -s tests/split

# Run a demo in one process: summary, plus CSV files in output/<scenario>/ next to it
python -m amber.run demo_experiments/tdma/tdma.toml
```

The conformance test runs each protocol with its default parameters on a four-Tag scenario:
- it checks the module defines `PROTOCOL`;
- it checks the source doesn't use the `random` module;
- it requires a split run to give exactly the same records as one process.

A test can only cover what the scenario reaches, so the rules above still matter for code paths it doesn't run.
