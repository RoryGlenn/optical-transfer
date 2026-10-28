"""Command-line packaging, playback, recovery, and checkpoint inspection."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import __version__
from .bundle import extract_verified, pack
from .errors import InvalidPacket, TransferError
from .packets import PROFILE, PROFILES, TOTAL_SHARDS, Packet, get_profile, hash_file
from .recovery import Recovery
from .video import play_video, read_qr, video_frames, write_video


def encode(source: Path, output: Path, rate: int, profile: int = PROFILE) -> dict[str, Any]:
    """Prepare an immutable archive and one repeatable transmission video."""
    coding = get_profile(profile)
    if output.exists() or output.is_symlink():
        raise TransferError(f"Transfer output already exists: {output}")
    if source.is_dir() and output.resolve().is_relative_to(source.resolve()):
        raise TransferError("The transfer output must be outside the input directory.")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".optical-encode-", dir=output.parent))
    last_update = time.monotonic()

    def progress(count: int) -> None:
        """Report useful progress while a potentially long video is rendered."""
        nonlocal last_update
        if time.monotonic() - last_update >= 2:
            print(f"Rendered {count} QR patterns...", file=sys.stderr)
            last_update = time.monotonic()

    try:
        inventory = pack(source, staging / "archive.zip")
        size = (staging / "archive.zip").stat().st_size
        frames = math.ceil(size / coding.group_size) * TOTAL_SHARDS
        print(
            f"Packed {size:,} bytes. One playback cycle: {frames / rate:.1f} seconds; "
            f"suggested initial recording: {2 * frames / rate + 5:.1f} seconds.",
            file=sys.stderr,
        )
        count = write_video(
            staging / "archive.zip", staging / "transfer.mp4", rate, progress, profile
        )
        metadata = {
            "version": 1,
            "profile": profile,
            "profile_name": coding.name,
            "sha256": hash_file(staging / "archive.zip").hex(),
            "archive_bytes": size,
            "files": sum(e["kind"] == "file" for e in inventory),
            "patterns": count,
            "patterns_per_second": rate,
            "cycle_seconds": count / rate,
            "suggested_recording_seconds": 2 * count / rate + 5,
        }
        (staging / "transfer.json").write_text(json.dumps(metadata, indent=2) + "\n")
        if output.exists() or output.is_symlink():
            raise TransferError(f"Transfer output appeared during encoding: {output}")
        staging.rename(output)
        return metadata
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def decode(videos: list[Path], state: Path, output: Path) -> tuple[int, dict[str, Any]]:
    """Import recordings, returning exit 0 for verified recovery or 2 if incomplete."""
    if output.exists() or output.is_symlink():
        raise TransferError(f"Output already exists: {output}")
    counters = {"frames": 0, "new_packets": 0, "invalid_packets": 0, "foreign_packets": 0}
    last_update = time.monotonic()
    with Recovery(state) as recovery:
        for video in videos:
            if recovery.complete:
                break
            if not video.is_file():
                raise TransferError(f"Recording does not exist: {video}")
            print(f"Reading {video}...", file=sys.stderr)
            with contextlib.closing(video_frames(video)) as frames:
                for image in frames:
                    counters["frames"] += 1
                    for raw in read_qr(image):
                        try:
                            packet = Packet.decode(raw)
                        except InvalidPacket:
                            counters["invalid_packets"] += 1
                            continue
                        result = recovery.add(packet)
                        if result == "new":
                            counters["new_packets"] += 1
                        elif result == "foreign":
                            counters["foreign_packets"] += 1
                    if time.monotonic() - last_update >= 2:
                        print(
                            f"{counters['frames']} frames; {len(recovery.done)}/{recovery.groups} "
                            "groups recovered...",
                            file=sys.stderr,
                        )
                        last_update = time.monotonic()
                    if recovery.complete:
                        break
        report = {**recovery.status(), **counters, "state": str(state.resolve())}
        if not recovery.complete:
            report["result"] = "incomplete"
            return 2, report
        archive = recovery.assemble()
        count = extract_verified(archive, output)
        report.update({"result": "verified", "files": count, "output": str(output.resolve())})
        return 0, report


def parser() -> argparse.ArgumentParser:
    """Build the public command-line interface."""
    root = argparse.ArgumentParser(description="Record animated QR codes and recover their files.")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    sender = commands.add_parser("encode", help="Package a file/folder and render a transfer video")
    sender.add_argument("source", type=Path)
    sender.add_argument(
        "--out", type=Path, required=True, help="New directory for the prepared transfer"
    )
    sender.add_argument("--rate", type=int, choices=(2, 3, 5, 6, 10), default=5)
    sender.add_argument(
        "--profile",
        choices=[p.name for p in PROFILES.values()],
        default="standard",
        help="robust uses less dense QR codes and takes about twice as long",
    )
    player = commands.add_parser("play", help="Play a transfer fullscreen in a loop; Escape stops")
    player.add_argument("transfer", type=Path, help="Prepared transfer directory or video file")
    player.add_argument("--windowed", action="store_true", help="Use a native-size window")
    receiver = commands.add_parser("decode", help="Recover files from one or more phone recordings")
    receiver.add_argument("videos", type=Path, nargs="+")
    receiver.add_argument(
        "--state", type=Path, required=True, help="Recovery cache, reused for more clips"
    )
    receiver.add_argument(
        "--out", type=Path, required=True, help="New directory for verified files"
    )
    status = commands.add_parser("status", help="Inspect saved packet recovery progress")
    status.add_argument("state", type=Path)
    return root


def main() -> int:
    """Execute a command with concise errors and stable completion exit codes."""
    arguments = parser().parse_args()
    try:
        if arguments.command == "encode":
            profile = next(p.identifier for p in PROFILES.values() if p.name == arguments.profile)
            report = encode(arguments.source, arguments.out, arguments.rate, profile)
            print(json.dumps(report, indent=2))
            print(f"Prepared: {arguments.out.resolve()}", file=sys.stderr)
            return 0
        if arguments.command == "play":
            path = arguments.transfer
            play_video(path / "transfer.mp4" if path.is_dir() else path, not arguments.windowed)
            return 0
        if arguments.command == "status":
            if not arguments.state.is_dir():
                raise TransferError(f"Recovery state does not exist: {arguments.state}")
            with Recovery(arguments.state) as recovery:
                print(json.dumps(recovery.status(), indent=2))
            return 0
        code, report = decode(arguments.videos, arguments.state, arguments.out)
        print(json.dumps(report, indent=2))
        if code == 2:
            print(
                "Incomplete: keep this state and import another recording with the same command.",
                file=sys.stderr,
            )
        return code
    except (TransferError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped. Saved recovery packets can be reused.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
