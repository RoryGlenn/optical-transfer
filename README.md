# Optical Transfer

Turn a file or folder into an animated QR video. Record the screen with a phone's
normal Camera app, then recover the files from the original recording on a computer.
The transfer payload travels through the screen and camera; both computers need
the decoder/encoder software installed beforehand.

This is a local prototype. Automated tests exercise real QR and video codecs,
packet loss, and resumable recovery. **A physical phone-to-screen transfer has not
yet been validated.** The initial settings favor reliability over speed.

## Install

Python 3.11–3.14 (use x64 Python 3.13 on Windows), [uv](https://docs.astral.sh/uv/), and FFmpeg (including `ffprobe`
and `ffplay`) are required. Tested environments are macOS arm64 with Python 3.13
and an Ubuntu 24.04 arm64 container with Python 3.12 and FFmpeg 6.1.1.

```sh
# macOS: install FFmpeg if it is missing
brew install ffmpeg

# Ubuntu 24.04: install FFmpeg if it is missing
sudo apt install ffmpeg
```

From this project directory, install a standalone command:

```sh
uv tool install --python 3.13 .
optical --help
```

The installed command works from any directory and does not depend on an editable
source import. To install an updated checkout, run
`uv tool install --reinstall --python 3.13 .` from the project directory.

## Windows 11 → iPhone recording → computer decoder

The iPhone is a recorder in this workflow. Its normal Camera app saves the video;
original files are reconstructed later on a computer running Optical Transfer.
No iPhone decoder app or live QR scanner is required.

Use **x64 Python 3.13** on Windows 11. The pinned recovery library has Windows x64
wheels for that interpreter; an ARM-native Python installation needs a source
build and is not the documented installation target. Install an FFmpeg build
that includes `ffmpeg.exe`, `ffprobe.exe`, `ffplay.exe`, `libx264`, and HEVC decoding,
and put its `bin` directory on PATH. Follow your computer's existing installation
process. Check the executables in PowerShell:

```powershell
py -3.13 -c "import platform, struct; print(platform.machine(), struct.calcsize('P') * 8)"
ffmpeg -version
ffprobe -version
ffplay -version
```

From the extracted project folder, create a local environment and install it.
Using executables directly avoids requiring PowerShell activation scripts:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: numpy zfec==1.6.0.0 zxing-cpp==3.1.1
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\optical.exe --help
```

Prepare a small folder, then display its transfer in a native-size window:

```powershell
.\.venv\Scripts\optical.exe encode "C:\Users\YourName\Desktop\to-send" --out "C:\Users\YourName\Desktop\prepared-transfer" --profile robust
.\.venv\Scripts\optical.exe play "C:\Users\YourName\Desktop\prepared-transfer" --windowed
```

On the iPhone, record normal video at 1080p/30 fps for the suggested duration.
For the first physical trial use ordinary SDR video with HDR disabled where the
option exists. Keep the whole QR and white border in focus and visible. Copy the
original MOV/MP4 to the decoder computer without messaging-app recompression.
H.264 and HEVC recordings, rotation metadata, and variable frame timing have
software tests; these tests do not reproduce physical focus or rolling shutter.

On a Windows decoder, the corresponding command is:

```powershell
.\.venv\Scripts\optical.exe decode "C:\Users\YourName\Downloads\IMG_1234.MOV" --state "C:\Users\YourName\Desktop\recovery" --out "C:\Users\YourName\Desktop\restored"
```

On macOS/Linux use the same `optical decode` command with local paths. Keep the
same recovery directory when importing additional recordings. Recovery locks use
Windows byte-range locking or POSIX flock as appropriate. Windows junctions and
symlinks are rejected, and Windows-reserved output filenames fail cleanly.

**Physical acceptance is still pending:** record a small known folder from the
actual Windows 11 display using the actual iPhone, decode its original video,
and compare every recovered file hash. Repeat with handheld capture before
relying on the path. Synthetic video or Windows CI cannot establish that result.

## Encode and record

```sh
optical encode /path/to/input-folder --out ./prepared-transfer
optical play ./prepared-transfer
```

The output directory must be new and outside the input folder. It contains:

- `archive.zip`: the frozen input bytes and integrity manifest; retain this with
  the video so future playback remains the same transfer.
- `transfer.mp4`: one transmission cycle; the player repeats it continuously.
- `transfer.json`: archive hash, size, file count, and playback duration.

The player opens fullscreen. Press **Escape** to stop playback. To request a
window at the video's native size instead of fullscreen enlargement:

```sh
optical play ./prepared-transfer --windowed
```

For a less dense QR code, encode using the lower-density fallback:

```sh
optical encode /path/to/input-folder --out ./robust-transfer --profile robust
```

The `robust` profile uses half as much data per QR and usually takes about twice
as long. Its name describes the intended tradeoff; physical camera reliability
still needs measurement. The decoder detects the profile from each packet.

1. Start ordinary rear-camera video recording, preferably 1080p at 30 fps.
2. Keep the complete QR and white border in frame, reasonably face-on and in focus.
3. Record for the suggested duration printed by `encode`: two cycles plus five
   seconds. This is an initial guideline, not a guarantee that enough data arrived.
4. Move the **original** MOV/MP4 recording to the computer running the decoder.
   A messaging service's recompressed copy may lose readable QR detail.

Playback can begin before recording. There is no required first QR frame. The
phone does not need a QR scanner application. No live success signal reaches the
sender, so completion is known only when the decoder verifies the recovered files.

## Decode and resume

```sh
optical decode /path/to/recording.mov --state ./recovery --out ./restored
```

`--out` must not already exist. Files appear there only after the archive and all
file hashes pass verification. A folder input restores its contents directly
inside `--out`; a single file restores under its original basename.

If a recording is incomplete, keep the recovery directory and import more clips:

```sh
optical status ./recovery
optical decode clip2.mov clip3.mp4 --state ./recovery --out ./restored
```

Repeated packets do not add progress. State is tied to one archive hash and coding
profile. Use a separate state directory for a different prepared transfer.
Playback of the existing video can be repeated at any time. Re-encoding a source
folder may create different archive bytes; retain and replay the original video.

Exit codes: `0` means the command succeeded; for `decode` it means verified file
recovery. `2` means decoding is incomplete and can resume. `1` indicates an error.
`130` indicates interruption. A `status` result of `ready_to_verify: true` means all
groups have been reconstructed, **not** that archive and file verification ran.

## Format and limits

Two compatible wire profiles are supported:

| Setting | `standard` (default) | `robust` |
|---|---:|---:|
| Wire profile | 1 | 2 |
| Payload per QR | 512 bytes | 256 bytes |
| Complete packet | 572 bytes | 316 bytes |
| QR format | Version 20-M | Version 15-M |
| Image at eight pixels/module | 840×840 | 680×680 |
| Original data per recovery group | 16 KiB | 8 KiB |

Both use a four-module quiet zone, 32 source shards and 16 repair shards per
group, with up to eight groups interleaved. Any 32 distinct correct shards recover
a group. Loss tolerance applies per group, not to an arbitrary loss pattern
across the entire video. QR correction handles damage within an image; the outer
code handles whole unreadable or missing packets.

A 30 fps H.264 video holds each pattern for six frames by default, giving five
new patterns per second. The player uses nearest-neighbor scaling; `--windowed`
requests the encoded dimensions. Actual display scaling, including OS pixel
density, still needs physical validation.

One standard cycle takes about 38 seconds for a 64 KiB packed archive, or
10.2 minutes for 1 MiB. Robust doubles those times; each profile rounds size up to
a recovery group. These are calculated playback times, not measured physical
transfer speeds. The recording may be much larger than the original files.
`encode --rate 2` slows playback; rates 3, 5, 6, and 10 are also supported but are
not independently calibrated capture profiles.

Existing standard recordings and recovery caches remain supported. Different
profiles use different shard geometry and cannot be combined in one recovery
cache, even if the archive hash matches. Use a separate `--state` for each profile.

Current limits: 32 MiB packed archive, 256 MiB expanded data, 10,000 entries, and a
4 MiB manifest. Only regular files and directories are accepted. Empty files,
empty directories, and supported Unicode names are preserved. Symlinks, special
files, permissions, extended attributes, and filesystem timestamps are outside
the preservation contract. Unsafe paths and case/Unicode-normalization collisions
are rejected. Existing output directories are not overwritten.

Recovery uses bounded working memory and local checkpoint files. It keeps packet
journals and recovered blocks on disk, so allow several times the archive size
for state and extraction. One process at a time can use a recovery directory.
The first matching valid packet binds an empty recovery directory to its transfer.
Other transfers are counted and ignored.

H.264/HEVC support depends on the installed FFmpeg build. Quarter-turn orientation
metadata is handled; non-quarter-turn metadata is rejected. Variable frame rates
are decoded without forcing a fixed frame rate. HDR, extreme perspective, glare,
motion blur, and heavily recompressed recordings have no physical validation yet.

SHA-256 and CRC provide corruption detection. The format does not encrypt data or
authenticate who sent it. Recovered files are never executed automatically.

## Protocol v1

All integer fields are big-endian. A standard QR holds 572 bytes; robust holds
316 bytes. Header fields and CRC semantics are identical:

| Field | Bytes | Value |
|---|---:|---|
| Magic | 4 | ASCII `OVT1` |
| Version, kind, profile, flags | 4 | `1, 1, 1 or 2, 0` |
| Archive SHA-256 | 32 | Digest of unpadded ZIP bytes |
| Archive length | 8 | Exact ZIP byte count |
| Group index | 4 | Zero-based |
| Shard index | 2 | 0–47 |
| Payload length | 2 | 512 (standard) or 256 (robust) |
| Payload | 512 or 256 | Original or repair shard |
| CRC-32 | 4 | IEEE CRC-32 over header plus payload |

Profile 1 fixes `zfec 1.6.0.0`'s systematic `(k=32, m=48)` mapping over equal-sized
512-byte buffers, source IDs 0–31 and repair IDs 32–47. Profile 2 uses the same
coding parameters with 256-byte buffers. The final group is padded
with zero bytes and trimmed using the archive length. `tests/test_packets.py`
freezes independent parity digests and checks 100 randomized recovery subsets
for each profile.
Changing the coding matrix or geometry requires a new profile.

The full archive hash and profile identify a transfer; filenames are only inside
the archive. No startup metadata frame is required. The archive has a JSON
`manifest.json` and entries under `payload/`. Each regular file's manifest record
contains its relative path, size, and SHA-256. Output is staged until verification
finishes.

## Development and validation

After changing source, install that snapshot into the development environment:

```sh
uv sync --locked --no-editable --reinstall-package optical-transfer
uv run --no-editable ruff check src tests
uv run --no-editable ruff format --check src tests
uv run --no-editable mypy src
uv run --no-editable pytest -q
uv build
git diff --check
```

Non-editable installs are intentional: this host can mark generated `.pth` files
hidden, which makes Python ignore editable package paths. Reinstall the project
before testing each changed source snapshot.

Tests marked `video` require actual FFmpeg and exercise generated H.264/MOV video,
mid-stream recording, orientation, and merging incomplete clips. They do not
simulate all screen/camera artifacts and do not substitute for filming a display.

Independent verification on macOS and Ubuntu 24.04 recovered both profiles of a
64 KiB incompressible fixture,
Unicode nested files, and empty files/directories from generated H.264. It also
recovered a standard-profile bundle on macOS from HEVC MOV with variable timestamps, 90-degree
rotation, mild perspective distortion, and blur. These are synthetic video
checks, not physical camera measurements. A clean wheel installation was tested
outside the source checkout. Profile-1 packet bytes and partial recovery caches
from versions 0.1.0 and 0.2.0 retain the same wire profiles in 0.3.0.

Windowed standard and fullscreen robust playback were also verified on an Xvfb
virtual display in Ubuntu: FFmpeg recorded the actual player output, and the
decoder restored the fixture bytes from those recordings. This verifies the
player path without claiming physical screen-to-phone camera validation.

Dependencies: [ZXing-C++](https://github.com/zxing-cpp/zxing-cpp) for raw QR bytes,
[zfec](https://github.com/tahoe-lafs/zfec) for erasure recovery,
[NumPy](https://numpy.org/) for image buffers, and [FFmpeg](https://ffmpeg.org/) for
video. zfec is available under GPL-2.0-or-later or TGPPL-1.0-or-later; its
distribution terms need to be accounted for before publishing a bundled product.
