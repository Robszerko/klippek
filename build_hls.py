#!/usr/bin/env python3
"""
KLIPEK TV HLS builder.

Reads MP4 files from the public Hugging Face dataset:
    androjid21/klippek

For each MP4:
  - downloads it
  - uses ffprobe to inspect duration
  - converts it to H.264/AAC MPEG-TS HLS segments
  - creates a compact manifest.json
  - uploads generated HLS assets to a separate hls/ directory
    in the same Hugging Face dataset.

The Worker consumes hls/manifest.json and generates a synchronized
live HLS playlist.

Environment:
  HF_TOKEN - Hugging Face write token
  HF_DATASET - optional, defaults to androjid21/klippek
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import requests

DATASET = os.environ.get("HF_DATASET", "androjid21/klippek")
HF_TOKEN = os.environ["HF_TOKEN"]
BRANCH = os.environ.get("HF_BRANCH", "main")
ROOT = Path(os.environ.get("HLS_ROOT", "hls"))
SEGMENT_SECONDS = int(os.environ.get("HLS_SEGMENT_SECONDS", "6"))
MAX_FILES = int(os.environ.get("MAX_FILES", "0"))

API = f"https://huggingface.co/api/datasets/{DATASET}/tree/{BRANCH}"
BASE_RESOLVE = f"https://huggingface.co/datasets/{DATASET}/resolve/{BRANCH}/"

HEADERS = {
    "Authorization": f"Bearer {HF_TOKEN}",
    "User-Agent": "klippek-tv-hls-builder/1.0",
}


def run(cmd, check=True):
    print("+", " ".join(map(str, cmd)), flush=True)
    return subprocess.run(cmd, check=check, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def list_mp4s():
    out = []
    cursor = None

    for _ in range(100):
        params = {
            "recursive": "true",
            "expand": "false",
            "limit": "1000",
        }
        if cursor:
            params["cursor"] = cursor

        r = requests.get(API, params=params, headers=HEADERS, timeout=60)
        r.raise_for_status()
        data = r.json()

        if isinstance(data, dict):
            items = data.get("items", [])
        else:
            items = data

        for item in items:
            if item.get("type") == "file":
                path = item.get("path", "")
                if path.lower().endswith(".mp4") and not path.startswith("hls/"):
                    out.append(path)

        link = r.headers.get("Link", "")
        m = re.search(r"[?&]cursor=([^>&, ]+)", link)
        if not m or len(items) < 1000:
            break
        cursor = m.group(1)

    out = sorted(set(out))
    if MAX_FILES:
        out = out[:MAX_FILES]
    return out


def download(path, target):
    url = BASE_RESOLVE + "/".join(quote(x, safe="") for x in path.split("/")) + "?download=true"
    with requests.get(url, headers=HEADERS, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(target, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)


def ffprobe_duration(path):
    p = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ])
    return float(p.stdout.strip())


def slug(path):
    name = Path(path).stem
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    # Include a stable hash-like path component so same-named files in folders
    # do not collide.
    import hashlib
    h = hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]
    return f"{name}_{h}"


def upload_file(local, remote):
    # Use HF Hub HTTP upload endpoint. PUT is supported by the Hub API.
    url = f"https://huggingface.co/api/datasets/{DATASET}/upload/{BRANCH}/{quote(remote, safe='/')}"
    with open(local, "rb") as f:
        r = requests.put(
            url,
            headers={**HEADERS, "Content-Type": "application/octet-stream"},
            data=f,
            timeout=600,
        )
    if r.status_code >= 300:
        raise RuntimeError(f"Upload failed {r.status_code}: {r.text[:1000]}")


def build_one(source, work, output_root):
    key = slug(source)
    outdir = output_root / key
    outdir.mkdir(parents=True, exist_ok=True)

    mp4 = work / "input.mp4"
    download(source, mp4)
    duration = ffprobe_duration(mp4)

    playlist = outdir / "index.m3u8"

    # Fixed GOP and normalized audio/video make the individual HLS segments
    # compatible when the Worker places multiple clips into one live playlist.
    run([
        "ffmpeg", "-hide_banner", "-y",
        "-i", str(mp4),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-vf", "scale=w=min(1280\,iw):h=-2:force_original_aspect_ratio=decrease",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-profile:v", "main",
        "-pix_fmt", "yuv420p",
        "-b:v", "2500k",
        "-maxrate", "2800k",
        "-bufsize", "5000k",
        "-r", "30",
        "-g", str(SEGMENT_SECONDS * 30),
        "-keyint_min", str(SEGMENT_SECONDS * 30),
        "-sc_threshold", "0",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",
        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_playlist_type", "vod",
        "-hls_segment_type", "mpegts",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", str(outdir / "seg_%05d.ts"),
        str(playlist),
    ])

    segments = []
    for seg in sorted(outdir.glob("seg_*.ts")):
        # Parse exact duration from ffprobe so the Worker can build accurate timing.
        d = run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(seg)
        ])
        sd = float(d.stdout.strip())
        segments.append({
            "duration": round(sd, 3),
            "path": f"{ROOT.as_posix()}/{key}/{seg.name}",
        })

    # Upload segments first, then playlist.
    for seg in sorted(outdir.glob("seg_*.ts")):
        upload_file(seg, f"{ROOT.as_posix()}/{key}/{seg.name}")

    return {
        "source": source,
        "title": Path(source).stem,
        "duration": round(duration, 3),
        "segments": segments,
    }


def main():
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise SystemExit("ffmpeg/ffprobe hiányzik.")

    sources = list_mp4s()
    print(f"Talált MP4-ek: {len(sources)}")

    if not sources:
        raise SystemExit("Nem találtam MP4 fájlt.")

    with tempfile.TemporaryDirectory(prefix="klippek-tv-") as td:
        work = Path(td)
        manifest_clips = []

        for n, source in enumerate(sources, 1):
            print(f"\n[{n}/{len(sources)}] {source}")
            try:
                item = build_one(source, work / f"clip_{n}", ROOT)
                manifest_clips.append(item)
            except Exception as e:
                print(f"SKIP: {source}: {e}", file=sys.stderr)

        if not manifest_clips:
            raise SystemExit("Egyetlen klipet sem sikerült feldolgozni.")

        manifest = {
            "version": 1,
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "segmentSeconds": SEGMENT_SECONDS,
            "clips": manifest_clips,
        }

        manifest_path = work / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        upload_file(manifest_path, f"{ROOT.as_posix()}/manifest.json")

        print("\nKÉSZ")
        print("Clips:", len(manifest_clips))
        print("Total seconds:", sum(x["duration"] for x in manifest_clips))


if __name__ == "__main__":
    main()
