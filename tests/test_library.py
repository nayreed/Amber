"""What Amber checks of OAI's C library (libaiot_phy.so) when it loads it, and what the library
refuses through Amber's binding: a library of another version, a second outcome for one D2R
window, an SDU larger than a device holds."""

import numpy as np
import pytest

from amber import link, mac


class _Size:
    """aiot_mac_struct_size() of a library built from other sources: one structure differs."""

    def __init__(self, real, name):
        self.real, self.name = real, name

    def __call__(self, type_name):
        size = self.real(type_name)
        return size + 8 if type_name == self.name.encode() else size


class _Lib:
    """The loaded library, with aiot_mac_struct_size() replaced, or missing when size is None."""

    def __init__(self, lib, size):
        self._lib, self._size = lib, size

    def __getattr__(self, name):
        if name == "aiot_mac_struct_size":
            if self._size is None:
                raise AttributeError(name)
            return self._size
        return getattr(self._lib, name)


@pytest.mark.parametrize("struct", ["aiot_mac_r2d_msg_t", "aiot_reader_heard_t", "aiot_device_upper_t"])
def test_a_library_of_another_version_is_refused(monkeypatch, struct):
    real = mac._L()
    monkeypatch.setattr(mac, "_lib", None)
    monkeypatch.setattr(mac.aiot, "load", lambda: _Lib(real, _Size(real.aiot_mac_struct_size, struct)))
    with pytest.raises(RuntimeError, match=f"{struct} is .* bytes, Amber's copy .*different versions"):
        mac._L()


def test_a_library_without_the_mac_is_refused(monkeypatch):
    real = mac._L()
    monkeypatch.setattr(mac, "_lib", None)
    monkeypatch.setattr(mac.aiot, "load", lambda: _Lib(real, None))
    with pytest.raises(RuntimeError, match="no A-IoT MAC"):
        mac._L()


def test_the_reader_takes_one_outcome_per_window():
    """X = 2: a paging opens two Msg1 windows. A second outcome for the first is refused, and the
    Reader keeps waiting for the second."""
    cfg = link.LinkConfig(m_chip_symb=6, n_rb=0, t_bit_tau="1/2", r_sfs=(1, 2), x=2, r_code=True, r_block=1,
                          l_amble=31, interval_cp=0, i_add=False, d2r_tbs=5, modulation="bpsk", timing_error=0.5,
                          tag_snr_db=10.0)
    reader = mac.Reader(cfg, "broadcast", {"occasions_exp": 3}, [1])
    assert reader.next_r2d(0) is not None
    first, second = reader.r2d_sent(0, 1000, 1000)
    reader.d2r_outcome(first, [], 0, 2000)
    with pytest.raises(ValueError, match="did not expect"):
        reader.d2r_outcome(first, [], 0, 2000)
    assert reader.next_r2d(2000) is None
    reader.d2r_outcome(second, [], 0, 3000)
    assert reader.next_r2d(3000) is not None


def test_an_sdu_larger_than_the_device_holds_raises():
    """The device's MAC keeps an upper layer SDU of at most 65536 bytes for segmentation (5.4.4);
    a larger one is an error raised from on_r2d, not a crash in C."""
    class Big(mac.UpperLayer):
        def next_sdu(self):
            return bytes(65537)
    dev = mac.Device(Big(), np.random.default_rng(1))
    cfa = mac.paging_cfa([1], mac.sched(mac.SCHED_PAGING_CFA, (8,), d2r_tbs=6, t_bit_tau=1))
    with pytest.raises(ValueError, match="more than the device holds"):
        dev.on_r2d(cfa)
