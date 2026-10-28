"""Bounded archive packaging and verified extraction into a new directory."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import unicodedata
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from .errors import TransferError
from .packets import MAX_BUNDLE_BYTES

MAX_FILES = 10_000
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def safe_name(value: str) -> str:
    """Validate a portable relative path without silently changing its name."""
    parts = value.split("/")
    if (
        not value
        or len(value.encode("utf-8")) > 1024
        or any(p in ("", ".", "..") for p in parts)
        or "\\" in value
        or any(ord(c) < 32 for c in value)
        or any(":" in p or p.endswith((" ", ".")) for p in parts)
    ):
        raise TransferError(f"Unsupported relative path: {value!r}")
    return value


def _validate_entries(entries: Any) -> list[dict[str, Any]]:
    """Validate inventory shape, path collisions, and extraction bounds."""
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        raise TransferError("Invalid manifest inventory or too many entries.")
    by_name: dict[str, dict[str, Any]] = {}
    folded: set[str] = set()
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise TransferError("Invalid manifest entry.")
        name = safe_name(entry["path"])
        canonical = unicodedata.normalize("NFC", name).casefold()
        if canonical in folded:
            raise TransferError(f"Duplicate or destination-colliding path: {name!r}")
        folded.add(canonical)
        if entry.get("kind") == "file":
            size = entry.get("size")
            digest = entry.get("sha256")
            if type(size) is not int or not 0 <= size <= MAX_EXPANDED_BYTES:
                raise TransferError("Invalid manifest file size.")
            if not isinstance(digest, str) or len(digest) != 64:
                raise TransferError("Invalid manifest digest.")
            try:
                bytes.fromhex(digest)
            except ValueError as error:
                raise TransferError("Invalid manifest digest.") from error
            total += size
        elif entry.get("kind") != "directory":
            raise TransferError("Only regular files and directories are supported.")
        by_name[name] = entry
    if total > MAX_EXPANDED_BYTES:
        raise TransferError("Expanded transfer exceeds 256 MiB.")
    for name in by_name:
        for parent in PurePosixPath(name).parents:
            if str(parent) != "." and by_name.get(str(parent), {}).get("kind") != "directory":
                raise TransferError(f"Missing or invalid parent directory for {name!r}")
    return list(by_name.values())


def _inventory(source: Path) -> list[tuple[str, Path, bool]]:
    """Inspect an input tree without following symlinks or special files."""
    if source.is_symlink():
        raise TransferError("Symlinks are not supported.")
    if source.is_file():
        return [(safe_name(source.name), source, False)]
    if not source.is_dir():
        raise TransferError("Input must be an existing regular file or directory.")
    result: list[tuple[str, Path, bool]] = []

    def walk_error(error: OSError) -> NoReturn:
        """Fail packaging instead of silently omitting unreadable directories."""
        raise error

    for root, dirs, files in os.walk(source, followlinks=False, onerror=walk_error):
        for name in sorted(dirs + files):
            path = Path(root) / name
            mode = path.lstat().st_mode
            is_dir = stat.S_ISDIR(mode)
            if not (is_dir or stat.S_ISREG(mode)):
                raise TransferError(f"Unsupported link or special file: {path}")
            result.append((safe_name(path.relative_to(source).as_posix()), path, is_dir))
            if len(result) > MAX_FILES:
                raise TransferError("Input exceeds 10000 entries.")
    return sorted(result)


def pack(source: Path, destination: Path) -> list[dict[str, Any]]:
    """Create a new ZIP snapshot and per-file integrity manifest.

    Parameters
    ----------
    source : Path
        A regular file or directory whose contents will be packed.
    destination : Path
        A new archive path outside the input directory.

    Returns
    -------
    list of dict
        The file and directory inventory embedded in the archive.
    """
    if destination.exists():
        raise TransferError(f"Destination already exists: {destination}")
    if source.is_dir() and destination.resolve().is_relative_to(source.resolve()):
        raise TransferError("The transfer output must be outside the input directory.")
    inventory = _inventory(source)
    entries: list[dict[str, Any]] = []
    total = 0
    created = False
    try:
        with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            created = True
            for name, path, is_dir in inventory:
                if is_dir:
                    archive.writestr(f"payload/{name}/", b"")
                    entries.append({"path": name, "kind": "directory"})
                    continue
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise TransferError(f"Input is no longer a regular file: {path}")
                    digest = hashlib.sha256()
                    count = 0
                    with archive.open(f"payload/{name}", "w") as member:
                        while chunk := stream.read(1024 * 1024):
                            count += len(chunk)
                            total += len(chunk)
                            if total > MAX_EXPANDED_BYTES:
                                raise TransferError("Input exceeds 256 MiB uncompressed.")
                            digest.update(chunk)
                            member.write(chunk)
                    after = os.fstat(stream.fileno())
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise TransferError(f"Input changed while packaging: {path}")
                    if count != before.st_size:
                        raise TransferError(f"Input changed while packaging: {path}")
                entries.append(
                    {"path": name, "kind": "file", "size": count, "sha256": digest.hexdigest()}
                )
            _validate_entries(entries)
            manifest = json.dumps({"version": 1, "entries": entries}, ensure_ascii=False).encode()
            if len(manifest) > MAX_MANIFEST_BYTES:
                raise TransferError("Manifest exceeds 4 MiB.")
            archive.writestr("manifest.json", manifest)
        if destination.stat().st_size > MAX_BUNDLE_BYTES:
            raise TransferError("Compressed archive exceeds the 32 MiB prototype limit.")
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return entries


def extract_verified(bundle: Path, destination: Path) -> int:
    """Verify and extract an archive atomically into a new destination.

    Parameters
    ----------
    bundle : Path
        Reconstructed ZIP with a version-one manifest.
    destination : Path
        A path that must not exist, including as a symlink.

    Returns
    -------
    int
        The number of verified regular files restored.
    """
    if destination.exists() or destination.is_symlink():
        raise TransferError(f"Output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".optical-extract-", dir=destination.parent))
    try:
        with zipfile.ZipFile(bundle) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES + 1 or len({i.filename for i in infos}) != len(infos):
                raise TransferError("Duplicate ZIP members or too many entries.")
            if any(
                i.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) for i in infos
            ):
                raise TransferError(
                    "Unsupported ZIP compression; only stored and deflated are accepted."
                )
            if any(i.flag_bits & 1 for i in infos):
                raise TransferError("Encrypted ZIP members are not supported.")
            info = archive.getinfo("manifest.json")
            if info.file_size > MAX_MANIFEST_BYTES:
                raise TransferError("Manifest exceeds 4 MiB.")
            manifest = json.loads(archive.read(info))
            if not isinstance(manifest, dict) or manifest.get("version") != 1:
                raise TransferError("Unsupported archive manifest.")
            entries = _validate_entries(manifest.get("entries"))
            expected = {"manifest.json"} | {
                f"payload/{e['path']}" + ("/" if e["kind"] == "directory" else "") for e in entries
            }
            if {i.filename for i in infos} != expected:
                raise TransferError("ZIP members do not match the manifest.")
            count = 0
            for entry in sorted(entries, key=lambda e: (e["path"].count("/"), e["path"])):
                name = entry["path"]
                is_dir = entry["kind"] == "directory"
                member_name = f"payload/{name}" + ("/" if is_dir else "")
                member_info = archive.getinfo(member_name)
                file_type = stat.S_IFMT(member_info.external_attr >> 16)
                if file_type not in (0, stat.S_IFDIR if is_dir else stat.S_IFREG):
                    raise TransferError("ZIP links and special files are not supported.")
                target = staging.joinpath(*name.split("/"))
                if is_dir:
                    if member_info.file_size != 0:
                        raise TransferError("Directory member contains data.")
                    target.mkdir()
                    continue
                if member_info.file_size != entry["size"]:
                    raise TransferError(f"Size mismatch for {name!r}")
                digest = hashlib.sha256()
                written = 0
                with archive.open(member_info) as stream, target.open("xb") as output:
                    while chunk := stream.read(1024 * 1024):
                        written += len(chunk)
                        if written > entry["size"]:
                            raise TransferError(f"Expanded size mismatch for {name!r}")
                        digest.update(chunk)
                        output.write(chunk)
                if written != entry["size"] or digest.hexdigest() != entry["sha256"]:
                    raise TransferError(f"File verification failed for {name!r}")
                count += 1
        if destination.exists() or destination.is_symlink():
            raise TransferError(f"Output appeared during extraction: {destination}")
        staging.rename(destination)
        return count
    except (
        zipfile.BadZipFile,
        KeyError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        NotImplementedError,
        RuntimeError,
        zlib.error,
        EOFError,
    ) as error:
        raise TransferError(f"Invalid transfer archive: {error}") from error
    finally:
        if staging.exists():
            shutil.rmtree(staging)
