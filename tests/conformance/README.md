# Spec conformance vectors for the Rel-19 PHY

- `vectors/` holds JSON test vectors taken from the 3GPP text: TS 38.291 V19.3.0, TS 38.391 V19.4.0, TS 38.191 V19.4.0 and TS 38.194 V19.3.0. `vectors/README.md` explains each file. Every vector says how directly the spec gives it (`spec-literal`, `spec-derived` or `spec-inferred`) and cites the clause (and for TS 38.291, 38.191 and 38.194 the page) it comes from.
- `aiot_conformance/` is the harness that runs the vectors, plus a reference oracle written only from the spec text. It shares no code with `amber.phy` or `amber.mac`.
- `amber_adapter.py` answers the vectors with `amber.phy` and `amber.mac` (OAI's C MAC through ctypes). `tests/test_phy.py::test_spec_conformance_vectors` runs it.

Run them by hand:

    PYTHONPATH=.:tests/conformance python3 -m aiot_conformance --vectors tests/conformance/vectors --impl amber_adapter:AmberAdapter
