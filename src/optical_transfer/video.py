"""FFmpeg transport and raw-byte QR images."""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Generator, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import zxingcpp
from numpy.typing import NDArray

from .errors import TransferError
from .packets import PROFILE, get_profile, iter_packets

IMAGE_SIZE = 840
VIDEO_FPS = 30


def executable(name: str) -> str:
    """Find a required executable or report the missing installation."""
    path = shutil.which(name)
    if path is None:
        raise TransferError(f"{name} is required. Install FFmpeg and ensure it is on PATH.")
    return path


def qr_image(data: bytes, profile: int = PROFILE) -> NDArray[np.uint8]:
    """Render a binary QR using eight-pixel modules and a four-module quiet zone."""
    coding = get_profile(profile)
    barcode = zxingcpp.create_barcode(
        data, zxingcpp.BarcodeFormat.QRCode, ec_level="M", version=coding.qr_version
    )
    result = np.asarray(barcode.to_image(scale=8, add_quiet_zones=True), dtype=np.uint8)
    if result.shape != (coding.image_size, coding.image_size):
        raise TransferError("QR library produced an unexpected image size.")
    return result


def read_qr(image: NDArray[np.uint8]) -> Iterator[bytes]:
    """Yield uninterpreted QR payloads, including non-text binary bytes."""
    for barcode in zxingcpp.read_barcodes(
        image, formats=zxingcpp.BarcodeFormats(zxingcpp.BarcodeFormat.QRCode)
    ):
        yield bytes(barcode.bytes)


def write_video(
    bundle: Path,
    destination: Path,
    updates: int = 5,
    progress: Callable[[int], None] | None = None,
    profile: int = PROFILE,
) -> int:
    """Write one H.264 transmission cycle from an immutable packed archive."""
    coding = get_profile(profile)
    if updates not in (2, 3, 5, 6, 10):
        raise TransferError("Pattern rate must be 2, 3, 5, 6, or 10 per second.")
    if destination.exists():
        raise TransferError(f"Video already exists: {destination}")
    command = [
        executable("ffmpeg"),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-s",
        f"{coding.image_size}x{coding.image_size}",
        "-r",
        "30",
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "12",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    count = 0
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=errors)
        assert process.stdin is not None
        try:
            for packet in iter_packets(bundle, profile):
                pixels = qr_image(packet.encode(), profile).tobytes()
                for _ in range(VIDEO_FPS // updates):
                    process.stdin.write(pixels)
                count += 1
                if progress:
                    progress(count)
            process.stdin.close()
            code = process.wait(timeout=120)
            if code:
                errors.seek(0)
                raise TransferError(
                    f"FFmpeg encoding failed: {errors.read(4096).decode(errors='replace')}"
                )
        except BaseException as error:
            process.kill()
            process.wait()
            with contextlib.suppress(BrokenPipeError):
                process.stdin.close()
            destination.unlink(missing_ok=True)
            if isinstance(error, BrokenPipeError):
                errors.seek(0)
                raise TransferError(
                    f"FFmpeg stopped encoding: {errors.read(4096).decode(errors='replace')}"
                ) from error
            raise
    return count


def probe_video(path: Path) -> dict[str, Any]:
    """Inspect dimensions and orientation before allocating frame buffers."""
    command = [
        executable("ffprobe"),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_streams",
        "-of",
        "json",
        str(path.resolve()),
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=True, timeout=30)
        streams = json.loads(result.stdout)["streams"]
        stream: dict[str, Any] = streams[0]
        width, height = int(stream["width"]), int(stream["height"])
        if not 1 <= width <= 8192 or not 1 <= height <= 8192 or width * height > 34_000_000:
            raise TransferError("Unsupported video dimensions (maximum 8K / 34 megapixels).")
        rotation = float(stream.get("tags", {}).get("rotate", 0))
        for side in stream.get("side_data_list", []):
            if "rotation" in side:
                rotation = float(side["rotation"])
        if round(rotation) % 90:
            raise TransferError("Only quarter-turn video orientation is supported.")
        if round(rotation) % 180:
            width, height = height, width
        return {"width": width, "height": height, "codec": stream.get("codec_name", "unknown")}
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
        KeyError,
        IndexError,
    ) as error:
        raise TransferError(f"Cannot inspect video {path}: {error}") from error


def video_frames(path: Path) -> Generator[NDArray[np.uint8], None, None]:
    """Decode every frame, allowing FFmpeg to apply the file's rotation metadata."""
    info = probe_video(path)
    width, height = info["width"], info["height"]
    frame_size = width * height
    command = [
        executable("ffmpeg"),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path.resolve()),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-fps_mode",
        "passthrough",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        assert process.stdout is not None
        try:
            while True:
                data = bytearray()
                while len(data) < frame_size:
                    part = process.stdout.read(frame_size - len(data))
                    if not part:
                        break
                    data.extend(part)
                if not data:
                    break
                if len(data) != frame_size:
                    raise TransferError("FFmpeg produced an incomplete video frame.")
                yield np.frombuffer(data, dtype=np.uint8).reshape(height, width)
            if process.wait(timeout=30):
                errors.seek(0)
                raise TransferError(
                    f"FFmpeg decoding failed: {errors.read(4096).decode(errors='replace')}"
                )
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait()


def play_video(path: Path, fullscreen: bool = True) -> None:
    """Open a looping player, optionally at native video size in a window."""
    if not path.is_file():
        raise TransferError(f"Video does not exist: {path}")
    geometry = ["-fs"]
    if not fullscreen:
        info = probe_video(path)
        geometry = ["-x", str(info["width"]), "-y", str(info["height"])]
    result = subprocess.run(
        [
            executable("ffplay"),
            "-loglevel",
            "error",
            *geometry,
            "-loop",
            "0",
            "-an",
            "-sws_flags",
            "neighbor",
            "-window_title",
            "Optical transfer - Escape to stop",
            str(path.resolve()),
        ]
    )
    if result.returncode:
        raise TransferError(f"Video player exited with status {result.returncode}.")
