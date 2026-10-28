"""Checkpoint persistence, deduplication, incomplete and integrity behavior."""

import random
from pathlib import Path

import pytest

from optical_transfer.errors import TransferError
from optical_transfer.packets import Packet, get_profile, iter_packets
from optical_transfer.recovery import Recovery


@pytest.mark.parametrize("profile", [1, 2])
def test_resume_missing_duplicates_and_foreign(tmp_path: Path, profile: int) -> None:
    source = tmp_path / "bundle"
    source.write_bytes(random.Random(1).randbytes(get_profile(profile).group_size * 2 + 232))
    packets = list(iter_packets(source, profile))
    state = tmp_path / "state"
    with Recovery(state) as recovery:
        for packet in packets:
            if packet.index < 16:
                assert recovery.add(packet) == "new"
                assert recovery.add(packet) == "duplicate"
        assert not recovery.complete
        assert recovery.status()["missing_shards_by_group"] == {"0": 16, "1": 16, "2": 16}
        with pytest.raises(TransferError, match="incomplete"):
            recovery.assemble()
    with Recovery(state) as recovery:
        foreign = Packet(b"f" * 32, 1, 0, 0, b"x" * 512)
        assert recovery.add(foreign) == "foreign"
        for packet in reversed(packets):
            if packet.index >= 32:
                recovery.add(packet)
        assert recovery.complete
        assert recovery.assemble().read_bytes() == source.read_bytes()


def test_same_archive_different_profile_is_not_merged(tmp_path: Path) -> None:
    source = tmp_path / "bundle"
    source.write_bytes(b"profile separation")
    standard = list(iter_packets(source))
    robust = list(iter_packets(source, 2))
    assert standard[0].digest == robust[0].digest
    with Recovery(tmp_path / "state") as recovery:
        recovery.add(robust[0])
        assert recovery.add(standard[0]) == "foreign"
        assert recovery.status()["profile"] == "robust"
    with Recovery(tmp_path / "state") as recovery:
        for packet in robust[1:32]:
            recovery.add(packet)
        assert recovery.assemble().read_bytes() == source.read_bytes()


def test_conflicting_packet_and_state_lock(tmp_path: Path) -> None:
    packet = Packet(b"d" * 32, 100, 0, 0, b"x" * 512)
    state = tmp_path / "state"
    with Recovery(state) as recovery:
        recovery.add(packet)
        with pytest.raises(TransferError, match="Another process"):
            Recovery(state)
        with pytest.raises(TransferError, match="Conflicting bytes"):
            recovery.add(Packet(packet.digest, packet.length, 0, 0, b"y" * 512))


def test_unrelated_state_directory_is_untouched(tmp_path: Path) -> None:
    original = tmp_path / "archive.zip"
    original.write_bytes(b"UNRELATED ORIGINAL FILE")
    with pytest.raises(TransferError, match="new/empty"):
        Recovery(tmp_path)
    assert original.read_bytes() == b"UNRELATED ORIGINAL FILE"
    assert {p.name for p in tmp_path.iterdir()} == {"archive.zip"}


def test_hash_failure_never_publishes_archive(tmp_path: Path) -> None:
    source = tmp_path / "bundle"
    source.write_bytes(b"hello")
    with Recovery(tmp_path / "state") as recovery:
        for p in iter_packets(source):
            recovery.add(Packet(b"z" * 32, p.length, p.group, p.index, p.payload))
        with pytest.raises(TransferError, match="SHA-256"):
            recovery.assemble()
        assert not (recovery.root / "archive.zip").exists()


def test_torn_journal_recovers_and_bad_checkpoint_rejects(tmp_path: Path) -> None:
    source = tmp_path / "bundle"
    source.write_bytes(b"hello")
    packets = list(iter_packets(source))
    state = tmp_path / "state"
    with Recovery(state) as recovery:
        recovery.add(packets[0])
    with (state / "packets-00000000.bin").open("ab") as stream:
        stream.write(b"partial")
    with Recovery(state) as recovery:
        for packet in packets[1:32]:
            recovery.add(packet)
        assert recovery.assemble().read_bytes() == b"hello"
    (state / "transfer.json").write_text("{}")
    with pytest.raises(TransferError, match="checkpoint"):
        Recovery(state)
