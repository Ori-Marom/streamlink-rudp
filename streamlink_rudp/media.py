"""Turn a video into a multi-bitrate, segmented stream (HLS-style MPEG-TS segments).

``prepare()`` encodes the source once per rendition of a bitrate ladder with keyframes
forced on every segment boundary, so segments from different renditions can be mixed
freely: the client may download segment 3 in 720p and segment 4 in 360p and the
concatenation is still a valid, playable stream.

The resulting directory contains one folder per rendition (with a standard HLS
``index.m3u8`` too) and a ``manifest.json`` that lists, for every segment, its duration,
size and SHA-256 so the client can verify each download.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class Rendition:
    name: str
    width: int
    height: int
    video_kbps: int
    audio_kbps: int = 96


DEFAULT_LADDER = (
    Rendition("360p", 640, 360, 700),
    Rendition("720p", 1280, 720, 2200),
    Rendition("1080p", 1920, 1080, 4500),
)

# Tiny ladder used by tests and the quick demo: encodes in seconds.
TINY_LADDER = (
    Rendition("180p", 320, 180, 150, 48),
    Rendition("360p", 640, 360, 450, 64),
)


class MediaError(RuntimeError):
    pass


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def _run(cmd: list[str]) -> None:
    log.debug("running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg failed ({proc.returncode}):\n{proc.stderr[-2000:]}")


def _require_ffmpeg() -> str:
    exe = ffmpeg_path()
    if not exe:
        raise MediaError("ffmpeg was not found on PATH. Install it from https://ffmpeg.org/download.html")
    return exe


def synthesize_source(path: Path, duration: float = 30.0, width: int = 1920, height: int = 1080,
                      fps: int = 30) -> Path:
    """Create a test video (moving test pattern with a running clock and a tone)."""
    exe = _require_ffmpeg()
    path.parent.mkdir(parents=True, exist_ok=True)
    _run([exe, "-y", "-hide_banner", "-loglevel", "error",
          "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps}",
          "-f", "lavfi", "-i", "sine=frequency=440:beep_factor=4:sample_rate=48000",
          "-t", str(duration), "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
          "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(path)])
    return path


def _label_filter(r: Rendition) -> str:
    text = f"{r.name}  {r.video_kbps} kbps"
    return (f"drawtext=text='{text}':fontcolor=white:fontsize=h/14:box=1:"
            f"boxcolor=black@0.55:boxborderw=12:x=w-tw-24:y=24")


def _encode(exe: str, source: Path, out: Path, r: Rendition, segment_seconds: float, label: bool) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("seg_*.ts"):
        old.unlink()
    vf = f"scale={r.width}:{r.height}:force_original_aspect_ratio=decrease," \
         f"pad={r.width}:{r.height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    if label:
        vf += "," + _label_filter(r)
    kbps = r.video_kbps
    _run([exe, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
          "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
          "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-pix_fmt", "yuv420p",
          "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.1)}k", "-bufsize", f"{kbps * 2}k",
          "-force_key_frames", f"expr:gte(t,n_forced*{segment_seconds})", "-sc_threshold", "0",
          "-c:a", "aac", "-b:a", f"{r.audio_kbps}k", "-ac", "2", "-ar", "48000",
          "-f", "hls", "-hls_time", str(segment_seconds), "-hls_playlist_type", "vod",
          "-hls_segment_filename", str(out / "seg_%04d.ts"), str(out / "index.m3u8")])


def _parse_playlist(path: Path) -> list[tuple[str, float]]:
    segments, duration = [], None
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            duration = float(line[8:].split(",")[0])
        elif line and not line.startswith("#"):
            segments.append((line, duration or 0.0))
            duration = None
    return segments


def prepare(source: Path, out_dir: Path, ladder=DEFAULT_LADDER, segment_seconds: float = 2.0,
            label: bool = True, title: str | None = None) -> dict:
    """Encode ``source`` into every rendition of ``ladder`` and write ``manifest.json``."""
    exe = _require_ffmpeg()
    source, out_dir = Path(source), Path(out_dir)
    if not source.is_file():
        raise MediaError(f"source video not found: {source}")
    out_dir.mkdir(parents=True, exist_ok=True)

    renditions = []
    for r in ladder:
        target = out_dir / r.name
        log.info("encoding %s (%dx%d @ %d kbps)", r.name, r.width, r.height, r.video_kbps)
        try:
            _encode(exe, source, target, r, segment_seconds, label)
        except MediaError:
            if not label:
                raise
            log.warning("text overlay unavailable (ffmpeg without drawtext/fonts); encoding without it")
            label = False
            _encode(exe, source, target, r, segment_seconds, label)
        segments = []
        for name, duration in _parse_playlist(target / "index.m3u8"):
            blob = (target / name).read_bytes()
            segments.append({"path": f"{r.name}/{name}", "duration": round(duration, 3),
                             "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()})
        total_bytes = sum(s["size"] for s in segments)
        total_time = sum(s["duration"] for s in segments) or 1.0
        renditions.append({
            "id": r.name, "width": r.width, "height": r.height,
            "nominal_kbps": r.video_kbps + r.audio_kbps,
            "bandwidth": int(total_bytes * 8 / total_time),  # measured average, bits/s
            "segments": segments,
        })

    counts = {len(r["segments"]) for r in renditions}
    if len(counts) != 1:
        raise MediaError(f"renditions produced different segment counts: {sorted(counts)}")
    renditions.sort(key=lambda r: r["bandwidth"])
    manifest = {
        "version": 1,
        "title": title or source.stem,
        "container": "mpegts",
        "segment_duration": segment_seconds,
        "duration": round(sum(s["duration"] for s in renditions[0]["segments"]), 3),
        "renditions": renditions,
    }
    (out_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))
    return manifest


def load_manifest(data: bytes) -> dict:
    manifest = json.loads(data)
    if manifest.get("version") != 1 or not manifest.get("renditions"):
        raise MediaError("unsupported or empty manifest")
    return manifest


def to_mp4(ts_path: Path, mp4_path: Path, renditions_used: set[str], height: int | None) -> Path:
    """Package the downloaded TS stream as MP4.

    If every segment came from one rendition this is a lossless remux. If the stream
    switched resolution, it is re-encoded at a single resolution, because many MP4
    players cannot handle a resolution change in the middle of a track.
    """
    exe = _require_ffmpeg()
    if len(renditions_used) <= 1 or not height:
        _run([exe, "-y", "-hide_banner", "-loglevel", "error", "-i", str(ts_path),
              "-c", "copy", "-movflags", "+faststart", str(mp4_path)])
    else:
        _run([exe, "-y", "-hide_banner", "-loglevel", "error", "-i", str(ts_path),
              "-vf", f"scale=-2:{height}:flags=lanczos,setsar=1", "-c:v", "libx264",
              "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy",
              "-movflags", "+faststart", str(mp4_path)])
    return mp4_path
