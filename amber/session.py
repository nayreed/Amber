# amber/session.py
"""The sessions amber-reader and amber-tag drive, one block per simulated millisecond.

The C programs exchange one block of IQ samples per millisecond over rfsim and pass every block
through these sessions: the Reader sends its waveform to all Tags and reads the sum of the Tags'
waveforms back, as rfsim gives it. In an rfsim_hub room whose carrier comes from a node of its own,
amber-reader runs that carrier node instead. The sessions themselves are in amber.iq; this
module is the entry point the C programs import, and it checks the scenario. When the run is over
each side writes its own CSV files (see amber.output).
"""

from . import iq
from .hosts import load_scenario


def _load(scenario_path: str):
    """The scenario, which must fix its seed: the Reader and every Tag must draw the same numbers."""
    scenario = load_scenario(scenario_path)
    if getattr(scenario, "seed_drawn", False):
        raise ValueError(f"{scenario_path}: the Reader and the Tags need a seed in [run], the same for all of them")
    return scenario


def ReaderSession(scenario_path: str, block_bytes: int, output_dir: str) -> iq.IqReaderSession:
    """The Reader's session for amber-reader."""
    return iq.IqReaderSession(_load(scenario_path), block_bytes, output_dir)


def CarrierSession(scenario_path: str, block_bytes: int, output_dir: str) -> iq.IqCarrierSession:
    """The carrier node's session, for amber-reader --amber.role carrier."""
    return iq.IqCarrierSession(_load(scenario_path), block_bytes, output_dir)


def TagSession(scenario_path: str, node_id: int, block_bytes: int, output_dir: str) -> iq.IqTagSession:
    """One Tag's session for amber-tag."""
    return iq.IqTagSession(_load(scenario_path), node_id, block_bytes, output_dir)
