# Amber's demos as amber.split scenarios

`broadcast.py`, `broadcast_sic.py`, `unicast.py` and `custom_protocol.py` are the four demos in
`demo_experiments/`, with the same nodes, base station, schedule or policy, capacitor, SIC setting
and duration. They differ in one way: each node draws from its own random generator
(`random.Random(f"{SEED}/{node_id}")`) instead of Python's global one, so a run split into a Reader
and one process per Tag gives exactly the results the same scenario gives in one process. The
demos themselves seed nothing (custom_protocol seeds only its node layout, which is kept), so
their own output files are one sample, not a reference.

One process, the reference:

    python3 -m amber.split.reference demo_experiments/split/broadcast.py out_ref

The demos' results (sent, decoded, DER, throughput) from any run's logs, or from a demo's output:

    python3 -m amber.split.summary 20001 out_ref demo_experiments/broadcast/output

custom_protocol runs 15001 ms; the others 20001 ms.
