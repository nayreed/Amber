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

The protocols are part of the A-IoT MAC library, OAI's `openair2/LAYER2/AIOT_MAC` (built into `libaiot_phy.so`, which Amber loads). It is the same code a gNB or a UE runs as the Reader, so a protocol tried in Amber runs unchanged in OAI. **A new protocol goes there**, in C.

- [How a protocol runs](#how-a-protocol-runs)
- [The protocols](#the-protocols)
- [Writing a new protocol](#writing-a-new-protocol)
- [Rules](#rules)
- [Pitfalls](#pitfalls)
- [Testing](#testing)

---

## How a protocol runs

### The Tag side (fixed)

Every Tag runs the device side of TS 38.391 from the MAC library (`aiot_mac_device.c`, through `amber.mac.Device`). In short, a Tag:

- answers an A-IoT Paging, either **CBRA** (contention-based random access, for everyone) or **CFA** (contention-free access, for one Paging ID);
- on a CBRA paging, draws a counter over the access occasions on offer and counts them down over the Access Triggers that follow;
- when its counter reaches an occasion, sends a 16-bit Random ID (**Msg1**) on that occasion's time slot and R_SFS;
- matches its Random ID in a **Random ID Response** (Msg2), takes the AS ID it is given, and sends its data in the entry's R_SFS;
- answers an R2D Upper Layer Data Transfer addressed to its AS ID with its data;
- drops its AS ID when it hears a NACK Feedback for it.

A Tag's Paging ID is its node id on 16 bits. Its **Data SDU** is its node id (16 bits) and its last sensed value (8 bits), so every D2R with data tells the Reader who sent it. A Tag only acts while its capacitor has enough charge. A Tag that is off misses what was sent meanwhile.

### The Reader side: a protocol

The Reader's MAC (`aiot_mac_reader.c`) runs the protocol. Each time the last R2D's answers are all in, it calls the protocol's step function with what that R2D brought back, and the step writes the next message:

```c
int step(void *state, const double *params, aiot_policy_ctx_t *ctx,
         const aiot_reader_heard_t *h1, const aiot_reader_heard_t *h2, aiot_policy_msg_t *next);
```

It returns 1 with `next` filled, or 0 when the protocol has ended. `state` is the protocol's own memory (8 KiB of zeros at the start), `params` its parameters in the order it declares them. `ctx` gives:

| Name | What it is |
|---|---|
| `ctx->cfg` | the scenario's `[phy]` table; `aiot_link_cbra()`, `aiot_link_cfa()` and `aiot_link_unicast()` make a message's Scheduling Info from it |
| `ctx->tid` | the last Transaction ID given (6 bits, wrapping) |
| `ctx->now_ms` | the Reader's clock, in ms |
| `ctx->node_ids`, `ctx->n_nodes` | the scenario's Tags |
| `ctx->add_row()` | adds a row to the protocol's table (`q.csv`, `adaptation.csv`) |

What comes back is an `aiot_reader_heard_t`:

- `answers`: the D2Rs decoded. Each has `kind` (`AIOT_ANSWER_MSG1` or `AIOT_ANSWER_DATA`), `message` (the Random ID, or the data with its SDU), `r_sfs`, `slot`, `as_id` and `rssi_w`;
- `collided`: preambles found whose frame did not decode (collisions, or a frame lost in noise).

A message marked `access` (a CBRA paging or an Access Trigger) has its Msg1s answered by the Reader itself, with one Random ID Response to at most `aiot_link_max_entries()` of them, each given the next AS ID. The step then gets both: `h1`, what the paging or trigger brought, and `h2`, what the Random ID Response brought (`NULL` when no Msg1 was decoded). Otherwise `h1` is what the message brought and `h2` is `NULL`.

The Reader sends the next message as soon as every answer the last one can cause has arrived, and no earlier than T_D2R_min after the latest D2R. There are no millisecond slots to plan: a D2R starts at T_R->D after the last chip of the R2D it answers (TS 38.291), and the protocol only chooses the order of messages.

### The messages a protocol can send

`next->msg` is an `aiot_mac_r2d_msg_t` (`aiot_mac_pdu.h`); set `kind` and the fields of that message:

| Message | `kind` and fields | What it does, and what it brings back |
|---|---|---|
| A-IoT Paging, CBRA | `AIOT_R2D_PAGING_CBRA`: `transaction_id`, `num_access_occasions_code` v, `sched` from `aiot_link_cbra()`; `access = 1` | Starts random access for every Tag that has not done this Transaction ID. Offers n = 2^v access occasions. Brings Msg1s. A Tag that already finished this Transaction ID ignores it, so a new one asks everyone again. |
| Access Trigger | `AIOT_R2D_ACCESS_TRIGGER`; `access = 1` | Opens the next block of access occasions of the last CBRA paging, and counts down every waiting Tag's counter. Brings Msg1s. |
| Random ID Response | sent by the Reader for an access message | Answers up to 8 Msg1s at once. Entry i tells that Tag to send on the i-th R_SFS. Gives each Tag an AS ID. Brings the data of every Tag answered. |
| A-IoT Paging, CFA | `AIOT_R2D_PAGING_CFA`: `paging_id` (`aiot_paging_id_of_node()`), `sched` from `aiot_link_cfa()` | Asks the one Tag with that Paging ID for data, with no contention. Brings its data. |
| R2D Upper Layer Data Transfer | `AIOT_R2D_UPPER_LAYER_DATA`: `as_id`, `sched` from `aiot_link_unicast()`, a Data SDU or a Received Data Size | Talks to one Tag by AS ID. With Received Data Size 0 it asks that Tag for data. With an SDU the Tag receives it (it appears in the Tag's `tag<id>_rx.csv`) and answers with data. |
| NACK Feedback | `AIOT_R2D_NACK_FEEDBACK`: `as_ids` | Tells the Tags with those AS IDs that their access failed. Each drops its AS ID and tries again at the next paging. A Tag only listens to it after its first data following a Random ID Response, and until it gets R2D data or a paging. Brings nothing. |

The protocol `manual` sends nothing of its own: its caller pushes every message (`aiot_reader_push`, `amber.mac.Reader.push`), as a core network's A-IoT function would ask a gNB. Amber's link tests use it (`amber.link.run_script`); a scenario cannot name it.

### Parameters of the link

The scenario's `[phy]` table is a `LinkConfig` (`amber/link.py`). Every key is required, so the scenario file alone states what is simulated; the docstring lists them: R2D chips per symbol, D2R bit duration, the R_SFS values a paging offers, time slots per R2D, D2R coding, the D2R transport block (`d2r_tbs`, for example 5 bytes: a 2-byte header and the 3-byte SDU) and so on. A protocol does not set these; it reads them through `ctx->cfg`. `[reader]` holds `protocol`, `params`, `enable_sic` and `noise_figure_db`.

---

## The protocols

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
- the protocol's table, if it keeps one: `adaptation.csv` and `q.csv` above.

`python -m amber.summary SIM_TIME_MS LOG_DIR` prints sent, decoded and collided packets, DER and throughput from these files.

---

## Writing a new protocol

A protocol is a step function in `openair2/LAYER2/AIOT_MAC/aiot_protocols.c` and an entry in its `PROTOCOLS` list:

```c
{.name = "stalest", .n_params = 0, .step = stalest_step},
```

An entry can also declare parameters (`param_names`, `param_defaults`, and `param_is_int` for those that take whole numbers only), a `check` function for their ranges, and a table (`table`, `columns`) the protocol fills with `ctx->add_row()`. Rebuild the library (`scripts/build_aiot_lib.sh /path/to/oai`) and a scenario can name it. There is no Tag code to write: whatever your protocol does, the Tags run the same 38.391 MAC.

Then add a demo: a folder `demo_experiments/stalest/` holding a scenario `stalest.toml` with `protocol = "stalest"` (copy another demo's file and change `[reader]`). `python -m amber.run demo_experiments/stalest/stalest.toml` runs it and writes its results next to it.

### What a protocol may decide

Reader decisions over 38.391 fields: how many access occasions to offer, whom to page and with which variant, when to send an Access Trigger, when to poll a known AS ID, when to NACK. Plus free content in the Data SDU of an R2D Upper Layer Data Transfer. It cannot add messages or change what a Tag does.

### Example: page the Tag heard longest ago

Each step pages, with CFA, the Tag whose data the Reader heard least recently, so a Tag that missed a turn is asked again first. A Tag that never has energy would be asked again at once every time and starve the others; add a back-off if that matters.

```c
/* -- stalest: page, with CFA, the Tag heard longest ago ------------------------------------- */

typedef struct {
  int started;
  aiot_sched_info_t sched;
  int k; /* the Tag paged last */
  double last[256]; /* when each Tag was last heard, in ms; -1: never */
} stalest_t;

static int stalest_step(void *state,
                        const double *p,
                        aiot_policy_ctx_t *ctx,
                        const aiot_reader_heard_t *h1,
                        const aiot_reader_heard_t *h2,
                        aiot_policy_msg_t *next)
{
  stalest_t *s = state;
  (void)p, (void)h2;
  if (ctx->n_nodes == 0 || ctx->n_nodes > 256)
    return 0; /* the protocol ends */
  if (!s->started) {
    s->started = 1;
    if (aiot_link_cfa(ctx->cfg, 0, 0, &s->sched) < 0)
      return 0;
    for (int i = 0; i < ctx->n_nodes; i++)
      s->last[i] = -1;
  } else if (h1 && h1->n_answers > 0) {
    s->last[s->k] = ctx->now_ms; /* the Tag paged last answered */
  }
  s->k = 0;
  for (int i = 1; i < ctx->n_nodes; i++)
    if (s->last[i] < s->last[s->k])
      s->k = i; /* ties go to the first in order */
  memset(next, 0, sizeof(*next));
  next->msg.kind = AIOT_R2D_PAGING_CFA;
  next->msg.sched = s->sched;
  next->msg.has_paging_id = 1;
  next->msg.paging_id_len = 16;
  aiot_paging_id_of_node(ctx->node_ids[s->k], next->msg.paging_id);
  return 1;
}

_Static_assert(sizeof(stalest_t) <= AIOT_PROTOCOL_STATE_BYTES, "stalest state");
```

For protocols that adapt, read `adaptive_aloha` (decides once per frame) and `gen2_q` (decides after every occasion) in the same file.

---

## Rules

1. **Write a message every step, or end.** Time only passes when the Reader sends something.
2. **Choose only what 38.391 leaves to the Reader.** Build the Scheduling Info with the `aiot_link_*()` helpers; a field the spec fixes (for example the R_SFS of an entry in a Random ID Response, which follows its position) is set by the library.
3. **Keep the protocol deterministic given what it hears.** The Reader and every Tag may run in separate containers on OAI (`amber-reader`, `amber-tag`), and the same scenario and seed must give the same files. A protocol draws no random numbers.
4. **Payloads are small.** A D2R carries `d2r_tbs` bytes (default 5), and the Tag's SDU is 3 of them.
5. **Parameters have types and ranges.** Declare them with their defaults, mark whole-number ones in `param_is_int`, and check ranges in `check`. Amber refuses a value of the wrong type when the scenario loads; the library refuses one out of range.
6. **Keep the state within 8 KiB**, and check it with a `_Static_assert`, as the example does.

A Tag hears an R2D only if it decodes it from the waveform, and a Tag's D2R reaches the Reader only if the Reader decodes it from the sum of every Tag's reflection. There is no formula for either: a Tag that is too far away does not hear, and two Tags on the same occasion collide.

---

## Pitfalls

- **A Transaction ID that did not change asks nobody.** A Tag that finished a CBRA with that Transaction ID ignores the paging. Take a new one for a new round, and reuse the ID only on purpose, as `registration` does.
- **Play out every occasion you offer.** A paging offering 2^v occasions with `m = x * len(r_sfs)` per R2D needs more R2D messages (Access Triggers) for the rest. Each R2D carries `x * len(r_sfs)` occasions. A Tag whose counter is past the occasions you play never answers in that round.
- **Answer Msg1s right away.** A Tag listens for its Random ID Response only for a short window after its Msg1 (K Access Triggers or pagings, K = 1 by default). The Reader answers an access message's Msg1s at once. A paging in between marks the Tag's access as failed.
- **A Random ID Response answers at most 8 Msg1s**, and no more than the R_SFS values the bit duration allows (`aiot_link_max_entries()`). The rest get no response and try again at the next paging.
- **A Tag that was off missed what was sent meanwhile.** Expect Tags whose counter or state is out of step, as with real devices.
- **Collisions are counted per R2D.** `Heard.collided` covers one message. There is no running total; keep your own, as `adaptive_aloha` does with its frame totals.

---

## Testing

From the repository root:

```bash
# Everything
PYTHONPATH=. python3 -m pytest tests

# The protocols (found by asking the library, so a new one is tested as soon as it is built in)
PYTHONPATH=. python3 -m pytest tests/test_protocols.py tests/test_protocol_conformance.py

# Run a demo: summary, plus the CSV files next to it
python -m amber.run demo_experiments/stalest/stalest.toml
```

`tests/test_protocols.py` runs each protocol and checks its parameters. `tests/test_protocol_conformance.py` checks every protocol the library lists. In OAI, `ctest -R test_aiot_mac` runs the Reader and every protocol against the device MAC over an ideal air, and the device MAC against 38.391 clause 5. A test can only cover what its scenario reaches, so the rules above still matter for code paths it doesn't run.
