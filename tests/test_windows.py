"""Windows-specific junction and installed command checks."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from optical_transfer.bundle import pack
from optical_transfer.errors import TransferError
from optical_transfer.recovery import Recovery


def test_cli_imports_and_starts() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "optical_transfer.cli", "encode", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--profile" in result.stdout


@pytest.mark.video
@pytest.mark.parametrize("profile", ["standard", "robust"])
def test_cli_spaced_unicode_binary_roundtrip(tmp_path: Path, profile: str) -> None:
    source = tmp_path / "input folder 日本語"
    (source / "empty directory").mkdir(parents=True)
    original = hashlib.shake_256(b"Windows to iPhone transfer fixture").digest(65536)
    (source / "binary file.bin").write_bytes(original)
    (source / "line endings.txt").write_bytes(b"one\r\ntwo\nthree\r\n\x1a\0")
    prepared = tmp_path / "prepared transfer"
    restored = tmp_path / "restored folder"
    encoded = subprocess.run(
        [
            sys.executable,
            "-m",
            "optical_transfer.cli",
            "encode",
            str(source),
            "--out",
            str(prepared),
            "--profile",
            profile,
        ],
        capture_output=True,
        timeout=120,
    )
    assert encoded.returncode == 0, encoded.stderr
    decoded = subprocess.run(
        [
            sys.executable,
            "-m",
            "optical_transfer.cli",
            "decode",
            str(prepared / "transfer.mp4"),
            "--state",
            str(tmp_path / "recovery state"),
            "--out",
            str(restored),
        ],
        capture_output=True,
        timeout=120,
    )
    assert decoded.returncode == 0, decoded.stderr
    assert (restored / "binary file.bin").read_bytes() == original
    assert (restored / "line endings.txt").read_bytes() == (
        source / "line endings.txt"
    ).read_bytes()
    assert (restored / "empty directory").is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows directory junctions")
def test_windows_junctions_are_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "outside.txt").write_text("outside the selected directory")
    source = tmp_path / "source"
    source.mkdir()
    junction = source / "junction"
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        check=True,
        timeout=10,
    )
    with pytest.raises(TransferError):
        pack(source, tmp_path / "archive.zip")
    with pytest.raises(TransferError):
        pack(junction, tmp_path / "archive2.zip")
    with pytest.raises(TransferError):
        Recovery(junction)
    assert (target / "outside.txt").read_text() == "outside the selected directory"
