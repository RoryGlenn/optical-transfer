"""Disk-backed, resumable packet recovery without retaining video frames."""

from __future__ import annotations

import json
import math
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any, BinaryIO

from .errors import TransferError
from .packets import (
    MAX_BUNDLE_BYTES,
    PROFILE,
    SOURCE_SHARDS,
    Packet,
    get_profile,
    hash_file,
    recover_group,
)
from .platform_io import is_link, lock_exclusive


def atomic_write(path: Path, data: bytes) -> None:
    """Publish a checkpoint using a sibling temporary file and atomic replace."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class Recovery:
    """Accumulate one archive's shards under an exclusively locked state directory.

    Parameters
    ----------
    root : Path
        Persistent local recovery cache, reused for additional recordings.
    """

    def __init__(self, root: Path) -> None:
        if (root.exists() or root.is_symlink()) and is_link(root.lstat()):
            raise TransferError("Recovery state must be a directory, not a link or junction.")
        root.mkdir(parents=True, exist_ok=True)
        marker = root / "optical-state.json"
        expected_marker = {"format": "optical-transfer-recovery", "version": 1}
        if any(root.iterdir()):
            try:
                if marker.is_symlink() or json.loads(marker.read_text()) != expected_marker:
                    raise ValueError("unrecognized state format")
            except (OSError, ValueError) as error:
                raise TransferError(
                    "Recovery state must be new/empty or an existing optical recovery cache. "
                    "No files were changed."
                ) from error
        else:
            with marker.open("x") as stream:
                json.dump(expected_marker, stream)
        self.root = root
        self._lock: BinaryIO = (root / ".lock").open("a+b")
        try:
            lock_exclusive(self._lock)
        except BlockingIOError as error:
            self._lock.close()
            raise TransferError("Another process is using this recovery directory.") from error
        except BaseException:
            self._lock.close()
            raise
        self.digest: bytes | None = None
        self.profile = PROFILE
        self.length = 0
        self.groups = 0
        self._cache: OrderedDict[int, dict[int, bytes]] = OrderedDict()
        self.done: set[int] = set()
        self.accepted = 0
        try:
            config = root / "transfer.json"
            if config.exists():
                data = json.loads(config.read_text())
                self.digest = bytes.fromhex(data["sha256"])
                self.length = data["length"]
                self.profile = data["profile"]
                coding = get_profile(self.profile)
                if (
                    len(self.digest) != 32
                    or type(self.length) is not int
                    or not 0 < self.length <= MAX_BUNDLE_BYTES
                ):
                    raise ValueError("invalid profile, digest, or size")
                self.groups = math.ceil(self.length / coding.group_size)
                for group in range(self.groups):
                    path = self._group_path(group)
                    if path.exists():
                        if path.stat().st_size != coding.group_size:
                            raise ValueError("invalid recovered group size")
                        self.done.add(group)
        except (ValueError, KeyError, TypeError, TransferError) as error:
            self.close()
            raise TransferError(f"Invalid recovery checkpoint: {error}") from error

    def __enter__(self) -> Recovery:
        """Enter the exclusive recovery session."""
        return self

    def __exit__(self, *_: object) -> None:
        """Release the recovery lock, including on errors."""
        self.close()

    def close(self) -> None:
        """Release the local state lock."""
        self._lock.close()

    def _group_path(self, group: int) -> Path:
        """Return the local path of a reconstructed group."""
        return self.root / f"group-{group:08d}.bin"

    def _shards(self, group: int) -> dict[int, bytes]:
        """Load an append-only journal, discarding a torn final packet if present."""
        if group in self._cache:
            self._cache.move_to_end(group)
            return self._cache[group]
        shards: dict[int, bytes] = {}
        journal = self.root / f"packets-{group:08d}.bin"
        packet_size = get_profile(self.profile).packet_size
        if journal.exists():
            with journal.open("r+b") as stream:
                while data := stream.read(packet_size):
                    if len(data) != packet_size:
                        stream.truncate(stream.tell() - len(data))
                        break
                    packet = Packet.decode(data)
                    if (
                        packet.digest != self.digest
                        or packet.profile != self.profile
                        or packet.length != self.length
                        or packet.group != group
                    ):
                        raise TransferError(
                            "Packet journal conflicts with the transfer checkpoint."
                        )
                    if packet.index in shards and shards[packet.index] != packet.payload:
                        raise TransferError("Conflicting packet data in recovery checkpoint.")
                    shards[packet.index] = packet.payload
        self._cache[group] = shards
        if len(self._cache) > 8:
            self._cache.popitem(last=False)
        return shards

    def add(self, packet: Packet) -> str:
        """Accept a packet and return new, duplicate, or foreign."""
        packet.validate()
        if self.digest is None:
            self.digest, self.length, self.groups = packet.digest, packet.length, packet.groups
            self.profile = packet.profile
            atomic_write(
                self.root / "transfer.json",
                json.dumps(
                    {
                        "profile": self.profile,
                        "sha256": self.digest.hex(),
                        "length": self.length,
                    }
                ).encode(),
            )
        if packet.digest != self.digest or packet.profile != self.profile:
            return "foreign"
        if packet.length != self.length:
            raise TransferError("Conflicting length for the same archive digest.")
        if packet.group in self.done:
            return "duplicate"
        shards = self._shards(packet.group)
        if packet.index in shards:
            if shards[packet.index] != packet.payload:
                raise TransferError("Conflicting bytes for the same group and shard.")
            result = "duplicate"
        else:
            journal = self.root / f"packets-{packet.group:08d}.bin"
            with journal.open("ab") as stream:
                stream.write(packet.encode())
                stream.flush()
                os.fsync(stream.fileno())
            shards[packet.index] = packet.payload
            self.accepted += 1
            result = "new"
        if len(shards) >= SOURCE_SHARDS:
            atomic_write(self._group_path(packet.group), recover_group(shards, self.profile))
            self.done.add(packet.group)
            self._cache.pop(packet.group, None)
        return result

    @property
    def complete(self) -> bool:
        """Return whether every group has enough data for archive verification."""
        return self.groups > 0 and len(self.done) == self.groups

    def status(self) -> dict[str, Any]:
        """Describe verified packet progress without claiming archive completion."""
        missing = {
            str(g): max(0, SOURCE_SHARDS - len(self._shards(g)))
            for g in range(self.groups)
            if g not in self.done
        }
        return {
            "profile": get_profile(self.profile).name if self.digest else None,
            "sha256": self.digest.hex() if self.digest else None,
            "archive_bytes": self.length,
            "recovered_groups": len(self.done),
            "total_groups": self.groups,
            "missing_shards_by_group": missing,
            "ready_to_verify": self.complete,
        }

    def assemble(self) -> Path:
        """Reconstruct the archive and require its complete SHA-256 to match."""
        if not self.complete:
            raise TransferError("The transfer is incomplete; import another recording.")
        temporary = self.root / "archive.zip.tmp"
        destination = self.root / "archive.zip"
        remaining = self.length
        with temporary.open("wb") as output:
            for group in range(self.groups):
                data = self._group_path(group).read_bytes()
                if len(data) != get_profile(self.profile).group_size:
                    raise TransferError("Recovered group is truncated.")
                chunk = data[:remaining]
                output.write(chunk)
                remaining -= len(chunk)
        if hash_file(temporary) != self.digest:
            temporary.unlink(missing_ok=True)
            raise TransferError("Archive SHA-256 mismatch; recovery state may be corrupt.")
        temporary.replace(destination)
        return destination
