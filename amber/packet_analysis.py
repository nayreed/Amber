# amber/packet_analysis.py
from __future__ import annotations
from typing import List, Tuple
import math
from .propagation import dbm_to_watts, lin_from_dbi


# Thermal noise constants
K_BOLTZMANN = 1.38e-23  # J/K
T_REF = 290.0  # Reference temperature (K)

def thermal_noise_watts(bandwidth_hz: float, noise_figure_db: float = 6.0) -> float:
    """Calculate thermal noise power: N = k * T * B * NF

    Parameters
    ----------
    bandwidth_hz : float
        Bandwidth in Hz
    noise_figure_db : float, optional
        Noise figure in dB, by default 6.0

    Returns
    -------
    float
        Thermal noise power in watts
    """    
    nf_lin = lin_from_dbi(noise_figure_db)
    return K_BOLTZMANN * T_REF * bandwidth_hz * nf_lin

# --------------------------------------------------------------------------------------
# SIC (Successive Interference Cancellation) algorithm
# --------------------------------------------------------------------------------------

def apply_sic(
    powers_dbm: List[float],
    noise_w: float,
    required_sinr_db: float,
    cancellation_factor: float = 0.9,
) -> Tuple[List[int], List[float]]:
    """Apply Successive Interference Cancellation (SIC) algorithm.

    SIC Algorithm:

    - Sort packets by power (strongest first)
    - Try to decode strongest packet (check SINR >= threshold)
    - If successful, subtract signal (with imperfect cancellation)
    - Re-calculate SINR for remaining packets
    - Repeat until no more packets can be decoded

    Parameters
    ----------
    powers_dbm : List[float]
        List of received powers at BS in dBm
    noise_w : float
        Thermal noise power in watts
    required_sinr_db : float
        Minimum SINR for successful decode
    cancellation_factor : float, optional
        Cancellation factor is how much of a decoded signal can be subtracted out of the received signal before trying to decode the next packet. 
        It's a value between 0 and 1, by default 0.9:

        1.0 = perfect cancellation → the decoded signal is fully removed, leaving zero residual interference
        0.9 = 90% cancellation → 10% of the decoded signal still leaks through as interference
        0.0 = no cancellation → the decoded signal stays fully in the mix (equivalent to capture effect only)

    Returns
    -------
    Tuple[List[int], List[float]]
        Indices of successfully decoded packets (in decode order) and their final SINR values
    """    
    n = len(powers_dbm)
    if n == 0:
        return [], []

    powers_w = [dbm_to_watts(p) for p in powers_dbm]
    residual_w = list(powers_w)  # Remaining power after cancellation
    decoded_indices: List[int] = []
    final_sinr_db = [-999.0] * n

    # Track which packets are still candidates
    candidates = set(range(n))

    while candidates:
        # Find strongest remaining candidate
        strongest_idx = max(candidates, key=lambda i: residual_w[i])
        signal_w = residual_w[strongest_idx]

        # Calculate interference from other candidates
        interference_w = sum(residual_w[j] for j in candidates if j != strongest_idx)

        # SINR for strongest
        sinr_lin = signal_w / max(1e-18, interference_w + noise_w)
        sinr_db = 10.0 * math.log10(max(1e-18, sinr_lin))
        final_sinr_db[strongest_idx] = sinr_db

        if sinr_db >= required_sinr_db:
            # Successfully decoded - cancel this signal
            decoded_indices.append(strongest_idx)
            candidates.remove(strongest_idx)

            # Imperfect cancellation: residual = original * (1 - cancellation_factor)
            residual_w[strongest_idx] = powers_w[strongest_idx] * (1.0 - cancellation_factor)
        else:
            # Cannot decode strongest - no more decoding possible
            # Record SINR for remaining candidates
            for idx in candidates:
                if idx != strongest_idx:
                    sig = residual_w[idx]
                    interf = sum(residual_w[j] for j in candidates if j != idx)
                    s_lin = sig / max(1e-18, interf + noise_w)
                    final_sinr_db[idx] = 10.0 * math.log10(max(1e-18, s_lin))
            break

    return decoded_indices, final_sinr_db
