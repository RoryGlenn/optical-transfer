"""Version-one binary framing and systematic Reed-Solomon shards."""

from __future__ import annotations

import hashlib
import math
import struct
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import zfec

from .errors import InvalidPacket, TransferError

MAGIC = b"OVT1"
VERSION = 1
PROFILE = 1
SHARD_SIZE = 512
SOURCE_SHARDS = 32
TOTAL_SHARDS = 48
GROUP_SIZE = SHARD_SIZE * SOURCE_SHARDS
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
HEADER = struct.Struct(">4sBBBB32sQIHH")
CRC = struct.Struct(">I")
PACKET_SIZE = HEADER.size + SHARD_SIZE + CRC.size


@dataclass(frozen=True)
class CodingProfile:
    """Fixed wire geometry and QR rendering parameters for one coding profile."""

    identifier: int
    name: str
    shard_size: int
    qr_version: int

    @property
    def group_size(self) -> int:
        """Return the unpadded capacity of a recovery group."""
        return self.shard_size * SOURCE_SHARDS

    @property
    def packet_size(self) -> int:
        """Return the complete binary packet length."""
        return HEADER.size + self.shard_size + CRC.size

    @property
    def image_size(self) -> int:
        """Return the QR image width including its four-module border."""
        return (17 + 4 * self.qr_version + 8) * 8


PROFILES = {
    1: CodingProfile(1, "standard", 512, 20),
    2: CodingProfile(2, "robust", 256, 15),
}


def get_profile(identifier: int) -> CodingProfile:
    """Resolve a supported profile without guessing unknown wire formats."""
    if type(identifier) is not int or identifier not in PROFILES:
        raise InvalidPacket("Unsupported coding profile.")
    return PROFILES[identifier]


@dataclass(frozen=True)
class Packet:
    """One independently identifiable shard of a packed archive.

    Parameters
    ----------
    digest : bytes
        SHA-256 of the complete unpadded archive.
    length : int
        Length of that archive in bytes.
    group : int
        Zero-based recovery group.
    index : int
        Shard index in the selected FEC profile.
    payload : bytes
        One fixed-size encoded shard.
    profile : int, optional
        Wire profile identifier; defaults to the original 512-byte profile.
    """

    digest: bytes
    length: int
    group: int
    index: int
    payload: bytes
    profile: int = PROFILE

    @property
    def groups(self) -> int:
        """Return the number of recovery groups in this transfer."""
        return math.ceil(self.length / get_profile(self.profile).group_size)

    @property
    def identity(self) -> str:
        """Return the stable archive/profile identifier."""
        return f"{self.digest.hex()}-p{self.profile}"

    def encode(self) -> bytes:
        """Serialize the header and payload with an IEEE CRC-32 trailer."""
        self.validate()
        data = (
            HEADER.pack(
                MAGIC,
                VERSION,
                1,
                self.profile,
                0,
                self.digest,
                self.length,
                self.group,
                self.index,
                len(self.payload),
            )
            + self.payload
        )
        return data + CRC.pack(zlib.crc32(data))

    def validate(self) -> None:
        """Reject unsupported sizes and out-of-range identifiers."""
        coding = get_profile(self.profile)
        if len(self.digest) != 32 or not 0 < self.length <= MAX_BUNDLE_BYTES:
            raise InvalidPacket("Invalid archive digest or size (maximum 32 MiB).")
        if not 0 <= self.group < self.groups or not 0 <= self.index < TOTAL_SHARDS:
            raise InvalidPacket("Invalid group or shard index.")
        if len(self.payload) != coding.shard_size:
            raise InvalidPacket("Invalid shard payload length.")

    @classmethod
    def decode(cls, data: bytes) -> Packet:
        """Parse a supported packet, rejecting corruption before using fields."""
        if len(data) not in {p.packet_size for p in PROFILES.values()}:
            raise InvalidPacket("Not a version-one optical packet.")
        if CRC.unpack(data[-4:])[0] != zlib.crc32(data[:-4]):
            raise InvalidPacket("Packet CRC mismatch.")
        magic, version, kind, profile, flags, digest, length, group, index, size = HEADER.unpack(
            data[: HEADER.size]
        )
        coding = get_profile(profile)
        if len(data) != coding.packet_size or (magic, version, kind, flags, size) != (
            MAGIC,
            VERSION,
            1,
            0,
            coding.shard_size,
        ):
            raise InvalidPacket("Unsupported packet version or profile.")
        packet = cls(digest, length, group, index, data[HEADER.size : -4], profile)
        packet.validate()
        return packet


def encode_group(data: bytes, profile: int = PROFILE) -> list[bytes]:
    """Return 32 original and 16 repair shards using the selected profile."""
    coding = get_profile(profile)
    if not 0 < len(data) <= coding.group_size:
        raise TransferError(f"A recovery group must contain 1 to {coding.group_size} bytes.")
    padded = data.ljust(coding.group_size, b"\0")
    shards = tuple(
        padded[i : i + coding.shard_size] for i in range(0, coding.group_size, coding.shard_size)
    )
    return [bytes(shard) for shard in zfec.Encoder(SOURCE_SHARDS, TOTAL_SHARDS).encode(shards)]


def recover_group(shards: Mapping[int, bytes], profile: int = PROFILE) -> bytes:
    """Recover a group from at least 32 distinct correct numbered shards."""
    coding = get_profile(profile)
    if len(shards) < SOURCE_SHARDS:
        raise TransferError("At least 32 distinct shards are required.")
    indices = sorted(shards)[:SOURCE_SHARDS]
    if any(not 0 <= i < TOTAL_SHARDS or len(shards[i]) != coding.shard_size for i in indices):
        raise TransferError("Invalid recovery shard.")
    recovered = zfec.Decoder(SOURCE_SHARDS, TOTAL_SHARDS).decode(
        tuple(shards[i] for i in indices), tuple(indices)
    )
    return b"".join(bytes(item) for item in recovered)


def hash_file(path: Path) -> bytes:
    """Compute a file digest with bounded memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def iter_packets(bundle: Path, profile: int = PROFILE) -> Iterator[Packet]:
    """Yield one pass, interleaving at most eight recovery groups at a time."""
    coding = get_profile(profile)
    length = bundle.stat().st_size
    if not 0 < length <= MAX_BUNDLE_BYTES:
        raise TransferError("Packed archive must be between 1 byte and 32 MiB.")
    digest = hash_file(bundle)
    groups = math.ceil(length / coding.group_size)
    with bundle.open("rb") as stream:
        for start in range(0, groups, 8):
            window = [
                encode_group(stream.read(coding.group_size), profile)
                for _ in range(min(8, groups - start))
            ]
            for index in range(TOTAL_SHARDS):
                for offset, shards in enumerate(window):
                    yield Packet(digest, length, start + offset, index, shards[index], profile)
