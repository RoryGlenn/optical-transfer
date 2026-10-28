"""Wire compatibility and recovery properties independent of video."""

import hashlib
import random
import struct
import zlib
from pathlib import Path

import pytest

from optical_transfer.errors import InvalidPacket, TransferError
from optical_transfer.packets import (
    GROUP_SIZE,
    HEADER,
    PACKET_SIZE,
    Packet,
    encode_group,
    get_profile,
    iter_packets,
    recover_group,
)


def test_binary_wire_and_crc() -> None:
    payload = bytes(range(256)) * 2
    packet = Packet(hashlib.sha256(payload).digest(), 512, 0, 47, payload)
    encoded = packet.encode()
    assert HEADER.size == 56
    assert PACKET_SIZE == len(encoded) == 572
    assert Packet.decode(encoded) == packet
    for index in (0, 5, 20, 55, 100, 571):
        broken = bytearray(encoded)
        broken[index] ^= 1
        with pytest.raises(InvalidPacket):
            Packet.decode(bytes(broken))
    for invalid in (b"", encoded[:-1], encoded + b"x"):
        with pytest.raises(InvalidPacket):
            Packet.decode(invalid)


def test_unknown_profile_and_invalid_bounds_with_valid_crc() -> None:
    raw = bytearray(Packet(b"a" * 32, 1, 0, 0, b"x" * 512).encode())
    raw[6] = 99
    raw[-4:] = struct.pack(">I", zlib.crc32(raw[:-4]))
    with pytest.raises(InvalidPacket, match="profile"):
        Packet.decode(bytes(raw))
    for packet in (
        Packet(b"a" * 32, 0, 0, 0, b"x" * 512),
        Packet(b"a" * 32, 1, 1, 0, b"x" * 512),
        Packet(b"a" * 32, 1, 0, 48, b"x" * 512),
    ):
        with pytest.raises(InvalidPacket):
            packet.encode()


def test_frozen_reed_solomon_vector_and_loss() -> None:
    data = hashlib.shake_256(b"optical-transfer-rs32-48-v1").digest(GROUP_SIZE)
    encoded = encode_group(data)
    assert hashlib.sha256(b"".join(encoded[32:])).hexdigest() == (
        "aa2e02e75a5247bd5c008c4beb25abf2f90d650285300d55a38abca79d649a35"
    )
    randomizer = random.Random(42)
    for _ in range(100):
        indices = randomizer.sample(range(48), 32)
        assert recover_group({i: encoded[i] for i in indices}) == data
    with pytest.raises(TransferError):
        recover_group(dict(enumerate(encoded[:31])))


def test_robust_wire_and_frozen_recovery_vector() -> None:
    coding = get_profile(2)
    data = hashlib.shake_256(b"optical-transfer-rs32-48-robust-v1").digest(coding.group_size)
    encoded = encode_group(data, 2)
    assert hashlib.sha256(b"".join(encoded[32:])).hexdigest() == (
        "73ac4771fbda8e23b3dc3355ec1182977ee49341d0604b1a52f67d6d3388b0c3"
    )
    packet = Packet(hashlib.sha256(data).digest(), len(data), 0, 47, encoded[47], 2)
    assert len(packet.encode()) == 316
    assert Packet.decode(packet.encode()) == packet
    assert packet.identity.endswith("-p2")
    randomizer = random.Random(2)
    for _ in range(100):
        indices = randomizer.sample(range(48), 32)
        assert recover_group({i: encoded[i] for i in indices}, 2) == data
    with pytest.raises(TransferError, match="Invalid recovery shard"):
        recover_group(dict(enumerate(encoded[:32])))


def test_interleaving_and_final_padding(tmp_path: Path) -> None:
    data = hashlib.shake_256(b"padding").digest(9 * GROUP_SIZE + 37)
    path = tmp_path / "archive"
    path.write_bytes(data)
    packets = list(iter_packets(path))
    assert len(packets) == 10 * 48
    assert [p.group for p in packets[:8]] == list(range(8))
    reconstructed = b"".join(
        recover_group({p.index: p.payload for p in packets if p.group == group and p.index >= 16})
        for group in range(10)
    )
    assert reconstructed[: len(data)] == data
    assert set(reconstructed[len(data) :]) == {0}
