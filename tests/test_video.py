"""Integrated QR/video paths using real codecs, without pretending to be camera tests."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from optical_transfer.cli import decode, encode
from optical_transfer.packets import Packet, get_profile
from optical_transfer.video import probe_video, qr_image, read_qr


@pytest.mark.parametrize("profile", [1, 2])
def test_qr_raw_bytes_and_geometry(profile: int) -> None:
    coding = get_profile(profile)
    payload = bytes(range(256)) * (coding.shard_size // 256)
    packet = Packet(bytes(range(32)), coding.shard_size, 0, 0, payload, profile).encode()
    image = qr_image(packet, profile)
    assert image.shape == (coding.image_size, coding.image_size)
    assert np.all(image[:32] == 255)
    assert list(read_qr(image)) == [packet]


@pytest.fixture(scope="module", params=[1, 2], ids=["standard", "robust"])
def transfer(tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest) -> Path:
    root = tmp_path_factory.mktemp("video")
    source = root / "source"
    (source / "nested" / "empty").mkdir(parents=True)
    (source / "nested" / "é.bin").write_bytes(bytes(range(256)) * 4)
    (source / "empty.txt").touch()
    metadata = encode(source, root / "prepared", 5, request.param)
    assert metadata["profile"] == request.param
    assert metadata["patterns"] == 48
    assert metadata["cycle_seconds"] == 9.6
    return root


def transcode(source: Path, destination: Path, *arguments: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(source),
            *arguments,
            "-an",
            str(destination),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


@pytest.mark.video
def test_generated_video_roundtrip(transfer: Path, tmp_path: Path) -> None:
    code, report = decode(
        [transfer / "prepared" / "transfer.mp4"], tmp_path / "state", tmp_path / "out"
    )
    assert code == 0 and report["result"] == "verified"
    assert (tmp_path / "out" / "nested" / "é.bin").read_bytes() == bytes(range(256)) * 4
    assert (tmp_path / "out" / "nested" / "empty").is_dir()
    assert (tmp_path / "out" / "empty.txt").read_bytes() == b""


@pytest.mark.video
def test_two_incomplete_clips_resume(transfer: Path, tmp_path: Path) -> None:
    video = transfer / "prepared" / "transfer.mp4"
    clip_a, clip_b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    transcode(
        video, clip_a, "-vf", "trim=start_frame=0:end_frame=120,setpts=PTS-STARTPTS", "-crf", "12"
    )
    transcode(
        video, clip_b, "-vf", "trim=start_frame=120:end_frame=240,setpts=PTS-STARTPTS", "-crf", "12"
    )
    state, out = tmp_path / "state", tmp_path / "out"
    code, first = decode([clip_a], state, out)
    assert code == 2 and first["new_packets"] == 20 and not out.exists()
    code, duplicate = decode([clip_a], state, out)
    assert code == 2 and duplicate["new_packets"] == 0
    code, last = decode([clip_b], state, out)
    assert code == 0 and last["result"] == "verified"


@pytest.mark.video
def test_middle_of_video_and_rotated_mov(transfer: Path, tmp_path: Path) -> None:
    video = transfer / "prepared" / "transfer.mp4"
    cropped = tmp_path / "middle.mp4"
    transcode(
        video,
        cropped,
        "-vf",
        "trim=start_frame=60,setpts=PTS-STARTPTS,pad=1000:840:80:0:white",
        "-crf",
        "12",
    )
    rotated = tmp_path / "rotated.mov"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-display_rotation",
            "90",
            "-i",
            str(cropped),
            "-c",
            "copy",
            str(rotated),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    info = probe_video(rotated)
    assert (info["width"], info["height"]) == (840, 1000)
    code, report = decode([rotated], tmp_path / "state", tmp_path / "out")
    assert code == 0 and report["result"] == "verified"
