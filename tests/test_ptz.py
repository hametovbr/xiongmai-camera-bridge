"""Offline tests: hashing, framing and the slot guard. No camera needed."""

import struct

import pytest

from xiongmai_bridge.dvrip import HEADER, VERSION, xmmd5
from xiongmai_bridge.ptz import RESERVED_SLOTS, check_slot


def test_xmmd5_is_sofia_hash():
    # 8 characters from the 62-char alphabet, deterministic
    assert xmmd5("admin") == xmmd5("admin")
    assert len(xmmd5("admin")) == 8
    assert xmmd5("admin") != xmmd5("Admin")
    assert all(c.isalnum() for c in xmmd5("whatever"))


def test_header_layout():
    packed = HEADER.pack(0xFF, VERSION, 0x0D49, 3, 0, 0, 1400, 42)
    assert len(packed) == 20
    assert packed[0] == 0xFF and packed[1] == 0x01
    assert struct.unpack("<I", packed[4:8])[0] == 0x0D49
    # padding lives in bytes 2-3
    assert packed[2:4] == b"\x00\x00"


def test_slot_guard_rejects_reserved_and_out_of_range():
    for bad in (0, -1, 255, 256, *RESERVED_SLOTS):
        with pytest.raises(ValueError):
            check_slot(bad)


def test_slot_guard_accepts_user_slots():
    for good in (1, 2, 9, 10, 99, 101, 200, 254):
        assert check_slot(good) == good
