# Protocols

Everything Amber puts on the air is 3GPP Rel-19 Ambient IoT. The Tag side is fixed by TS 38.391 (the A-IoT MAC) and is the same for every protocol. A protocol is only a **Reader policy**: it chooses which 38.391 messages the Reader sends, and with which fields. It cannot invent messages or change how a Tag behaves.

A scenario file picks a protocol by name and sets its parameters:

```toml
[reader]
protocol = "gen2_q"
enable_sic = true

[reader.params]
q0 = 4.0
c = 0.3
```

Any parameter you leave out keeps its default. A parameter the protocol doesn't have, or a value of the wrong type, is refused when the scenario loads.

This folder holds every protocol. **A new protocol goes here too**, as one Python file.

- [How a protocol runs](#how-a-protocol-runs)
- [The protocols in this folder](#the-protocols-in-this-folder)
- [Writing a new protocol](#writing-a-new-protocol)
- [Rules](#rules)
- [Pitfalls](#pitfalls)
- [Testing](#testing)

---

## How a protocol runs

### The Tag side (fixed)

Every Tag runs `DeviceMAC` (`amber/mac/procedures.py`), the device side of TS 38.391. In short, a Tag:

- answers an A-IoT Paging, either **CBRA** (contention-based random access, for everyone) or **CFA** (contention-free access, for one Paging ID);
- on a CBRA paging, draws a counter over the access occasions on offer and counts them down over the Access Triggers that follow;
- when its counter reaches an occasion, sends a 16-bit Random ID (**Msg1**) on that occasion's time slot and R_SFS;
- matches its Random ID in a **Random ID Response** (Msg2), takes the AS ID it is given, and sends its data in the entry's R_SFS;
- answers an R2D Upper Layer Data Transfer addressed to its AS ID with its data;
- drops its AS ID when it hears a NACK Feedback for it.

A Tag's Paging ID is its node id on 16 bits. Its **Data SDU** is its node id (16 bits) and its last sensed value (8 bits), so every D2R with data tells the Reader who sent it. A Tag only acts while its capacitor has enough charge. A Tag that is off misses what was sent meanwhile.

### The Reader side: a policy

`Protocol.policy(ctx)` is a generator. It **yields** one R2D message at a time and **receives** what that message brought back:

```python
heard = yield ctx.mac.paging_cfa(paging_id(3), ctx.cfg.cfa())
```

`ctx` gives the policy:

| Name | What it is |
|---|---|
| `ctx.mac` | a `ReaderMAC`: builds the messages and keeps the AS IDs it has handed out (`ctx.mac.assigned`) |
| `ctx.cfg` | the scenario's `[phy]` table, a `LinkConfig`; its helpers `cbra()`, `ridr(n)`, `cfa()` and `unicast()` make the Scheduling Info of a message |
| `ctx.next_tid()` | a fresh 6-bit Transaction ID (wraps after 63) |
| `ctx.now_ms` | the Reader's clock, in ms |
| `ctx.node_ids` | the scenario's Tags (also `self.node_ids` on the protocol) |
| `ctx.tags_in(heard)` | `(node id, value)` of every Tag whose data is in `heard` |

What comes back is a `amber.link.Heard`:

- `answers`: the D2Rs decoded. Each has `kind` (`"msg1"` or `"data"`), `message` (the `AccessRandomID`, or the data transfer with its `sdu`), `r_sfs`, `slot`, `as_id` and `rssi_w`;
- `collided`: preambles found whose frame did not decode (collisions, or a frame lost in noise);
- `empty`: true when there is neither.

The Reader sends the next message as soon as every answer the last one can cause has arrived. There are no millisecond slots to plan: a D2R starts at T_R->D after the last chip of the R2D it answers (TS 38.291), and the policy only chooses the order of messages.

### The messages a policy can send

| Message | Build it with | What it does, and what it brings back |
|---|---|---|
| A-IoT Paging, CBRA | `ctx.mac.paging_cbra(tid, ctx.cfg.cbra(), n_occasions_code=v)` | Starts random access for every Tag that has not done this Transaction ID. Offers n = 2^v access occasions. Brings Msg1s (`kind == "msg1"`). A Tag that already finished this Transaction ID ignores it, so a new `tid` asks everyone again. |
| Access Trigger | `mp.AccessTrigger()` (`from ..mac import pdu as mp`) | Opens the next block of access occasions of the last CBRA paging, and counts down every waiting Tag's counter. Brings Msg1s. |
| Random ID Response | `ctx.mac.random_id_response(heard, ctx.cfg.ridr(len(heard)))` | Answers up to 8 Msg1s at once. Entry i tells that Tag to send on the i-th R_SFS. Gives each Tag an AS ID. Brings the data of every Tag answered (`kind == "data"`). |
| A-IoT Paging, CFA | `ctx.mac.paging_cfa(paging_id, ctx.cfg.cfa())` | Asks the one Tag with that Paging ID for data, with no contention. Brings its data. |
| R2D Upper Layer Data Transfer | `ctx.mac.data(as_id, ctx.cfg.unicast(), sdu=None, received=0)` | Talks to one Tag by AS ID. With `received=0` and no SDU it asks that Tag for data. With an `sdu` the Tag receives it (it appears in the Tag's `tag<id>_rx.csv`) and answers with data. |
| NACK Feedback | `ctx.mac.nack([as_id, ...])` | Tells the Tags with those AS IDs that their access failed. Each drops its AS ID and tries again at the next paging. A Tag only listens to it after its first data following a Random ID Response, and until it gets R2D data or a paging. Brings nothing. |

Two helpers on `Protocol` build on these. Use them with `yield from`:

- `self.access(ctx, msg)` sends a CBRA paging or an Access Trigger, then answers every Msg1 it brought (up to `ctx.cfg.max_entries`) with one Random ID Response. It returns `(h1, h2)`; `h2` is `None` when no Msg1 was decoded.
- `self.r2d_per_paging(ctx, v, sched)` is how many R2D messages (the paging and its Access Triggers) play out 2^v occasions. Each R2D carries `x * len(r_sfs)` occasions. `self.occasions_code(n)` gives the codepoint v for at least n occasions.

### Parameters of the link

The scenario's `[phy]` table is a `LinkConfig` (`amber/link.py`). Every key is required, so the scenario file alone states what is simulated; the docstring lists them: R2D chips per symbol, D2R bit duration, the R_SFS values a paging offers, time slots per R2D, D2R coding, the D2R transport block (`d2r_tbs`, for example 5 bytes: a 2-byte header and the 3-byte SDU) and so on. A protocol does not set these; it reads them through `ctx.cfg`. `[reader]` holds `protocol`, `params`, `enable_sic` and `noise_figure_db`.

---

## The protocols in this folder

### `broadcast`

**What it is.** The simplest scheme: page everyone, answer everyone heard.

**One frame:** a CBRA paging with a new Transaction ID, offering 2^`occasions_exp` access occasions, then as many Access Triggers as it takes to play them out. After each, the Reader answers the Msg1s it decoded with a Random ID Response, which brings their data.

Every Tag that hears the paging and has energy sends a Msg1 on a random occasion. Two Tags on the same occasion and R_SFS collide. With SIC on, the stronger one may still be decoded.

| Parameter | Default | Meaning |
|---|---|---|
| `occasions_exp` | 2 | v: the paging offers 2^v access occasions, v in [0, 15] |

**Use it for:** a few Tags, or testing energy and radio settings. With more Tags than occasions it collides a lot, because the number never changes. Demos: `demo_experiments/broadcast/`.

### `unicast`

**What it is.** Round-robin polling, one Tag at a time, with no contention.

**One frame:** for each Tag in the scenario's order, a CFA paging carrying that Tag's Paging ID. Only that Tag answers, with its data.

No parameters.

**Use it for:** a collision-free baseline. A frame is as long as the number of Tags, so with many Tags each one is asked rarely. A Tag with no energy at its turn misses it. Demo: `demo_experiments/unicast/`.

### `registration`

**What it is.** Tags join once, then the Reader polls each by AS ID.

The Reader keeps **one Transaction ID for the whole run**. A Tag that has joined ignores later pagings with it (38.391 §5.2). A Tag whose access failed (its Msg1 collided, or it got no Random ID Response) tries again at the next paging (§5.5.3).

**One frame:**
1. **Join.** A CBRA paging offering 2^`occasions_exp` occasions, and its Access Triggers. Every Msg1 decoded is answered with a Random ID Response that gives the Tag an AS ID and brings its data.
2. **Poll.** An R2D Upper Layer Data Transfer to every AS ID given out so far (received size 0), which asks that Tag alone for data.

| Parameter | Default | Meaning |
|---|---|---|
| `occasions_exp` | 2 | v for the join paging, in [0, 15] |

**Use it for:** how long Tags take to join, or anything that needs a join step. Many Tags joining at once collide a lot. Demo: `demo_experiments/registration/`.

### `adaptive_aloha`

**What it is.** Framed ALOHA whose number of access occasions follows the load. It decides once per frame.

**One frame:** a CBRA paging with a new Transaction ID offering n = 2^v occasions, and its Access Triggers; every Msg1 decoded is answered with a Random ID Response.

**How v adapts.** After each frame the policy:
1. counts the Msg1s it decoded and the collisions;
2. smooths that load: `load = (1 - smoothing) * load + smoothing * (heard + collided)`;
3. moves v one step towards log2(`occasions_per_contender` * load), within [`min_exp`, `max_exp`].

v starts at `start_exp`.

| Parameter | Default | Meaning |
|---|---|---|
| `start_exp` | 4 | v in the first frame |
| `min_exp`, `max_exp` | 1, 6 | the range for v, within [0, 15] |
| `smoothing` | 0.25 | weight of the newest frame in the load, in (0, 1] |
| `occasions_per_contender` | 2 | occasions per estimated contending Tag |

**Output.** `adaptation.csv` with `time_s, n_occasions, heard, collided` for every frame.

**Use it for:** many Tags whose number is unknown or changes. Demo: `demo_experiments/adaptive_aloha/`.

### `gen2_q`

**What it is.** EPC Gen2's Q algorithm on 38.391. It decides after **every access occasion**.

The mapping is:

| Gen2 | 38.391 |
|---|---|
| Query (with Q) | CBRA paging offering 2^Q access occasions, with one occasion per R2D message (`x = 1`, one R_SFS) |
| QueryRep | Access Trigger |
| RN16 | Msg1 |
| ACK | Random ID Response (it also brings the Tag's data) |

**One round:** the paging with a new Transaction ID, then 2^Q - 1 Access Triggers. Each Tag draws a counter from 0 to 2^Q - 1; each trigger counts it down; the Tag whose counter reaches its occasion sends its Msg1.

**How Q adapts.** After every occasion the policy updates a running value `Qf` (it starts at `q0`): `+ c` after a collision (or more than one Msg1), `- c` after an empty occasion, unchanged after a success. `Qf` stays within [0, 15] and the next round uses `Q = round(Qf)`.

| Parameter | Default | Meaning |
|---|---|---|
| `q0` | 4.0 | starting Qf, in [0, 15] |
| `c` | 0.3 | step size, in (0, 1] |

**Output.** `q.csv` with `time_s, q, decoded, collided` for every occasion.

**Use it for:** an algorithm that reacts after every occasion. The cost is an R2D message in front of every occasion, as with Gen2's QueryRep. Demo: `demo_experiments/gen2_q/`.

### Choosing

| You want | Use |
|---|---|
| A few Tags, simplest possible | `broadcast` |
| No collisions, Tags asked in turn | `unicast` |
| Tags must join before sending data | `registration` |
| Many Tags, occasions adapt, decided per frame | `adaptive_aloha` |
| Many Tags, decided after every occasion | `gen2_q` |

### Output files

Every run writes (see `amber/output.py` and `amber/summary.py`):

- `reader_tx.csv`: every R2D message the Reader sent, with start and end time and its main fields;
- `reader_rx.csv`: every D2R it decoded, and every collision, with the Tag's node id and value from its Data SDU, the Random ID or AS ID, R_SFS, time slot and RSSI;
- `tag<id>_tx.csv`: every D2R that Tag started;
- `tag<id>_rx.csv`: every R2D Data SDU that Tag received;
- the files the protocol adds with `Protocol.outputs()`: `adaptation.csv` and `q.csv` above.

`python -m amber.summary SIM_TIME_MS LOG_DIR` prints sent, decoded and collided packets, DER and throughput from these files.

---

## Writing a new protocol

Add one file, for example `amber/protocols/stalest.py`. It needs:

1. **A `Protocol` subclass** (from `.base`) with:
   - `PARAMS`: parameter names and their defaults. The default's type is the type a value must have: write `5` for an integer and `0.5` for a number.
   - `policy(self, ctx)`: the generator.
   - Optionally `check()` to validate values (`self.positive("n")` requires at least 1, `self.within("v", 0, 15)` a range), and `outputs()` for extra CSV files, as `{file name: (columns, rows)}`.
2. **`PROTOCOL = YourClass`** at the end of the file.

There is no Tag class to write. Whatever your protocol does, the Tags run the same 38.391 MAC.

Then add a demo: a folder `demo_experiments/stalest/` holding a scenario `stalest.toml` with `protocol = "stalest"` (copy another demo's file and change `[reader]`). `python -m amber.run demo_experiments/stalest/stalest.toml` runs it and writes its results next to it.

### What a custom protocol may decide

Reader decisions over 38.391 fields: how many access occasions to offer, whom to page and with which variant, when to send an Access Trigger, which Msg1s to answer and which AS IDs to give, when to poll a known AS ID, when to NACK. Plus free content in the Data SDU of an R2D Upper Layer Data Transfer. It cannot add messages or change what a Tag does.

### Example: page the Tag heard longest ago

Each step pages, with CFA, the Tag whose data the Reader heard least recently, so a Tag that missed a turn is asked again first. A Tag that never has energy would be asked again at once every time and starve the others; add a back-off if that matters.

```python
# amber/protocols/stalest.py
from .base import Protocol


class Stalest(Protocol):
    PARAMS = {}

    def policy(self, ctx):
        from ..iq import paging_id
        sched = ctx.cfg.cfa()
        last = {node: float("-inf") for node in self.node_ids}   # when each was last heard, in ms
        while True:
            node = min(self.node_ids, key=last.get)              # ties go to the first in order
            heard = yield ctx.mac.paging_cfa(paging_id(node), sched)
            for heard_node, _value in ctx.tags_in(heard):
                last[heard_node] = ctx.now_ms


PROTOCOL = Stalest
```

### Example: NACK a join that got no data

A Random ID Response gives an AS ID to each Msg1 it answers. If a Tag's data never arrives, NACK its AS ID, so the Tag drops it and joins again at the next paging. This is the body of a join step, inside a policy:

```python
first = ctx.mac.next_as_id
h1, h2 = yield from self.access(ctx, msg)                 # msg: a CBRA paging or an Access Trigger
if h2 is not None:
    got = {a.as_id for a in h2.answers}
    lost = [i for i in range(first, ctx.mac.next_as_id) if i not in got]
    if lost:
        yield ctx.mac.nack(lost)
```

For policies that adapt, read `adaptive_aloha.py` (decides once per frame) and `gen2_q.py` (decides after every occasion).

---

## Rules

1. **Yield at least one message in every pass of your loop.** Time only passes when the Reader sends something. A loop that yields nothing never ends.
2. **Choose only what 38.391 leaves to the Reader.** Build messages with `ctx.mac` and the Scheduling Info with `ctx.cfg`; a field the spec fixes (for example the R_SFS of an entry in a Random ID Response, which follows its position) is set by the helper.
3. **Keep the policy deterministic given what it hears.** The Reader and every Tag may run in separate containers on OAI (`amber-reader`, `amber-tag`), and the same scenario and seed must give the same files. If you need randomness in the policy, use your own `random.Random(...)` with a fixed seed, not the `random` module's global functions. The conformance test reads each module for these.
4. **Payloads are small.** A D2R carries `d2r_tbs` bytes (default 5), and the Tag's SDU is 3 of them.
5. **Parameters have types and ranges.** Declare them in `PARAMS` with a default of the right type, and validate ranges in `check()`.

A Tag hears an R2D only if it decodes it from the waveform, and a Tag's D2R reaches the Reader only if the Reader decodes it from the sum of every Tag's reflection. There is no formula for either: a Tag that is too far away does not hear, and two Tags on the same occasion collide.

---

## Pitfalls

- **A Transaction ID that did not change asks nobody.** A Tag that finished a CBRA with that Transaction ID ignores the paging. Call `ctx.next_tid()` for a new round, and reuse the ID only on purpose, as `registration` does.
- **Play out every occasion you offer.** A paging offering 2^v occasions with `m = x * len(r_sfs)` per R2D needs more R2D messages (Access Triggers) for the rest. `self.r2d_per_paging` counts them. A Tag whose counter is past the occasions you play never answers in that round.
- **Answer Msg1s right away.** A Tag listens for its Random ID Response only for a short window after its Msg1 (K Access Triggers or pagings, K = 1 by default). `self.access` answers at once. A paging in between marks the Tag's access as failed.
- **A Random ID Response answers at most 8 Msg1s**, and no more than the R_SFS values the bit duration allows (`ctx.cfg.max_entries`). The rest get no response and try again at the next paging.
- **A Tag that was off missed what was sent meanwhile.** Expect Tags whose counter or state is out of step, as with real devices.
- **Collisions are counted per R2D.** `Heard.collided` covers one message. There is no running total; keep your own, as `adaptive_aloha` does with its frame totals.

---

## Testing

From the repository root:

```bash
# Everything
PYTHONPATH=. python3 -m unittest discover -s tests

# The protocols (found by listing this folder, so a new file is tested as soon as it is there)
PYTHONPATH=. python3 -m unittest tests/test_protocols.py tests/test_protocol_conformance.py

# Run a demo: summary, plus the CSV files next to it
python -m amber.run demo_experiments/stalest/stalest.toml
```

`tests/test_protocols.py` runs each protocol and checks its parameters. `tests/test_protocol_conformance.py` runs every module in this folder on one machine and as separate Reader and Tag sessions, and expects the same records. The 38.391 Tag behaviour itself is covered by the `tests/test_phy*.py, tests/test_mac*.py, tests/test_link.py` files. A test can only cover what its scenario reaches, so the rules above still matter for code paths it doesn't run.
