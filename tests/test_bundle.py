"""Filesystem roundtrips and rejection of unsafe or inconsistent archives."""

import hashlib
import json
import struct
import sys
import zipfile
from pathlib import Path

import pytest

from optical_transfer.bundle import extract_verified, pack
from optical_transfer.cli import encode, main
from optical_transfer.errors import TransferError
from optical_transfer.packets import iter_packets
from optical_transfer.recovery import Recovery


def test_nested_unicode_empty_files_and_directories(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "nested" / "empty").mkdir(parents=True)
    (source / "nested" / "café-日本語.bin").write_bytes(bytes(range(256)))
    (source / "blank").touch()
    archive = tmp_path / "archive.zip"
    pack(source, archive)
    output = tmp_path / "restored"
    assert extract_verified(archive, output) == 2
    assert (output / "nested" / "empty").is_dir()
    assert (output / "nested" / "café-日本語.bin").read_bytes() == bytes(range(256))
    assert (output / "blank").read_bytes() == b""
    with pytest.raises(TransferError, match="exists"):
        extract_verified(archive, output)


def test_single_file_and_empty_root(tmp_path: Path) -> None:
    source = tmp_path / "file.txt"
    source.write_text("hello")
    pack(source, tmp_path / "file.zip")
    extract_verified(tmp_path / "file.zip", tmp_path / "out")
    assert (tmp_path / "out" / "file.txt").read_text() == "hello"
    empty = tmp_path / "empty"
    empty.mkdir()
    pack(empty, tmp_path / "empty.zip")
    assert extract_verified(tmp_path / "empty.zip", tmp_path / "empty-out") == 0


def test_pack_rejects_symlink_collision_and_recursive_output(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "link").symlink_to("missing")
    with pytest.raises(TransferError):
        pack(source, tmp_path / "bad.zip")
    (source / "link").unlink()
    with pytest.raises(TransferError, match="outside"):
        pack(source, source / "bad.zip")
    # The host may use a case-insensitive filesystem; validate collisions on imported ZIP instead.


@pytest.mark.parametrize("names", [["../escape"], ["/absolute"], ["a\\b"], ["A", "a"]])
def test_malicious_manifest_names(tmp_path: Path, names: list[str]) -> None:
    archive = tmp_path / "bad.zip"
    entries = [
        {"path": name, "kind": "file", "size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
        for name in names
    ]
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("manifest.json", json.dumps({"version": 1, "entries": entries}))
        for name in names:
            stream.writestr("payload/" + name, b"x")
    with pytest.raises(TransferError):
        extract_verified(archive, tmp_path / "out")
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "escape").exists()


def test_digest_mismatch_does_not_publish(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("payload/file", b"bad")
        stream.writestr(
            "manifest.json",
            json.dumps(
                {
                    "version": 1,
                    "entries": [
                        {
                            "path": "file",
                            "kind": "file",
                            "size": 3,
                            "sha256": "00" * 32,
                        }
                    ],
                }
            ),
        )
    with pytest.raises(TransferError, match="verification"):
        extract_verified(archive, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_recursive_output_does_not_create_parent(tmp_path: Path) -> None:
    source = tmp_path / "input"
    source.mkdir()
    with pytest.raises(TransferError, match="outside"):
        encode(source, source / "new-parent" / "transfer", 5)
    assert list(source.iterdir()) == []


@pytest.mark.parametrize("problem", ["unsupported", "encrypted", "deflate"])
def test_unsupported_archive_is_clean_cli_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    problem: str,
) -> None:
    archive = tmp_path / "unsupported.zip"
    compression = zipfile.ZIP_DEFLATED if problem == "deflate" else zipfile.ZIP_STORED
    with zipfile.ZipFile(archive, "w", compression=compression) as stream:
        stream.writestr("manifest.json", '{"version":1,"entries":[]}')
    raw = bytearray(archive.read_bytes())
    local = raw.index(b"PK\x03\x04")
    central = raw.index(b"PK\x01\x02")
    if problem == "encrypted":
        struct.pack_into("<H", raw, local + 6, 1)
        struct.pack_into("<H", raw, central + 8, 1)
    elif problem == "unsupported":
        struct.pack_into("<H", raw, local + 8, 99)
        struct.pack_into("<H", raw, central + 10, 99)
    else:
        name_length, extra_length = struct.unpack_from("<HH", raw, local + 26)
        raw[local + 30 + name_length + extra_length] = 0xFF
    archive.write_bytes(raw)
    state = tmp_path / "state"
    with Recovery(state) as recovery:
        for packet in iter_packets(archive):
            recovery.add(packet)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "optical",
            "decode",
            "unused-video.mov",
            "--state",
            str(state),
            "--out",
            str(tmp_path / "out"),
        ],
    )
    assert main() == 1
    captured = capsys.readouterr()
    assert "Error:" in captured.err and "Traceback" not in captured.err
    assert not (tmp_path / "out").exists()
