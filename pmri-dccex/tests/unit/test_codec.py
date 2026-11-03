from __future__ import annotations

import pytest

from pmri_dccex.codec import DccexMessage, decode, encode, read_message


def test_encode_decode_round_trip() -> None:
    msg = DccexMessage(command="T", args=("12", "1"))
    raw = encode(msg)
    assert raw == b"<T 12 1>"
    assert decode("T 12 1") == msg


def test_command_only() -> None:
    msg = DccexMessage(command="1", args=())
    assert encode(msg) == b"<1>"
    assert decode("1") == DccexMessage(command="1", args=())


def test_decode_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        decode("")
    with pytest.raises(ValueError, match="empty"):
        decode("   ")


class _Src:
    def __init__(self, data: bytes) -> None:
        self._data = list(data)
        self._i = 0

    async def read_byte(self) -> int:
        if self._i >= len(self._data):
            raise EOFError
        b = self._data[self._i]
        self._i += 1
        return b


async def test_read_message_strips_brackets() -> None:
    src = _Src(b"<T 12 1>")
    text = await read_message(src.read_byte)
    assert text == "T 12 1"


async def test_read_message_skips_noise_between_messages() -> None:
    src = _Src(b"\r\nstatus blah\n<p 1>")
    text = await read_message(src.read_byte)
    assert text == "p 1"


async def test_read_message_reads_one_at_a_time() -> None:
    src = _Src(b"<1><0><p 1>")
    assert await read_message(src.read_byte) == "1"
    assert await read_message(src.read_byte) == "0"
    assert await read_message(src.read_byte) == "p 1"
