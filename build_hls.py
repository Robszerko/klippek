#!/usr/bin/env python3
"""
KLIPEK TV — FAST HLS REMUX BUILDER

Source:
  androjid21/klippek

Important:
  This version does NOT re-encode the MP4 files.
  FFmpeg uses -c copy and only repackages compatible streams
  into MPEG-TS HLS segments.

The per-clip index.m3u8 is uploaded last and acts as the
completion marker, so interrupted runs can resume.

Only H.264 video + AAC audio are accepted by the fast path.
Clips using another codec are reported as SKIP instead of
being silently re-encoded.
"""

import hashlib
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
from huggingface_hub import HfApi, CommitOperationAdd


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
    "User-Agent": "klippek-tv-fast-remux/2.0",
}

HF = HfApi(token=HF_TOKEN)


def run(cmd):
    print("+", " ".join(map(str, cmd)), flush=True)
    return subprocess.run(
        cmd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def encode_path(path):
    return "/".join(quote(x, safe="") for x in path.split("/"))


def resolve_url(path):
    return BASE_RESOLVE + encode_path(path) + "?download=true"


def list_repo_files():
    files = set()
    cursor = None

    for _ in range(300):
        params = {
            "recursive": "true",
            "expand": "false",
            "limit": "1000",
        }
        if cursor:
            params["cursor"] = cursor

        r = requests.get(
            API,
            params=params,
            headers=HEADERS,
            timeout=90,
        )
        r.raise_for_status()

        data = r.json()
        items = data.get("items", []) if isinstance(data, dict) else data

        for item in items:
            if item.get("type") == "file" and item.get("path"):
                files.add(item["path"])

        link = r.headers.get("Link", "")
        m = re.search(r"[?&]cursor=([^>&, ]+)", link)

        if not m or len(items) < 1000:
            break

        cursor = m.group(1)

    return files


def parse_hls_playlist(text):
    result = []
    pending = None

    for raw in text.splitlines():
        line = raw.strip()

        if not line:
            continue

        if line.startswith("#EXTINF:"):
            try:
                pending = float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                pending = None
            continue

        if line.startswith("#"):
            continue

        if pending is not None:
            result.append((line, pending))
            pending = None

    return result


def slug(path):
    name = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        Path(path).stem,
    )
    digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]
    return f"{name}_{digest}"


def list_mp4s(repo_files):
    result = sorted(
        p for p in repo_files
        if p.lower().endswith(".mp4")
        and not p.startswith("hls/")
    )

    if MAX_FILES:
        result = result[:MAX_FILES]

    return result


def download(source, target):
    print(f"Downloading: {source}", flush=True)

    with requests.get(
        resolve_url(source),
        headers=HEADERS,
        stream=True,
        timeout=180,
    ) as r:
        r.raise_for_status()

        with open(target, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)


def probe(path):
    p = run([
        "ffprobe",
        "-v", "error",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name",
        "-of", "json",
        str(path),
    ])

    data = json.loads(p.stdout)

    video = next(
        (
            s for s in data.get("streams", [])
            if s.get("codec_type") == "video"
        ),
        None,
    )

    audio = next(
        (
            s for s in data.get("streams", [])
            if s.get("codec_type") == "audio"
        ),
        None,
    )

    if not video:
        raise RuntimeError("Nincs video stream.")

    duration = float(
        data.get("format", {}).get("duration") or 0
    )

    if duration <= 0:
        raise RuntimeError("Érvénytelen videóidőtartam.")

    return {
        "duration": duration,
        "video": video.get("codec_name", ""),
        "audio": audio.get("codec_name", "") if audio else "",
    }


def commit_files(files, message):
    operations = [
        CommitOperationAdd(
            path_in_repo=remote,
            path_or_fileobj=str(local),
        )
        for local, remote in files
    ]

    HF.create_commit(
        repo_id=DATASET,
        repo_type="dataset",
        revision=BRANCH,
        operations=operations,
        commit_message=message,
    )


def remote_clip_item(source, repo_files):
    key = slug(source)
    prefix = f"{ROOT.as_posix()}/{key}/"
    index_remote = prefix + "index.m3u8"

    if index_remote not in repo_files:
        return None

    r = requests.get(
        resolve_url(index_remote),
        headers=HEADERS,
        timeout=60,
    )

    if r.status_code == 404:
        return None

    r.raise_for_status()

    refs = parse_hls_playlist(r.text)

    if not refs:
        return None

    for filename, _duration in refs:
        if prefix + filename not in repo_files:
            return None

    return {
        "source": source,
        "title": Path(source).stem,
        "duration": round(sum(d for _, d in refs), 3),
        "segments": [
            {
                "duration": round(d, 3),
                "path": prefix + filename,
            }
            for filename, d in refs
        ],
    }


def build_one(source, work, output_root):
    key = slug(source)
    outdir = output_root / key

    if outdir.exists():
        shutil.rmtree(outdir, ignore_errors=True)

    outdir.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    mp4 = work / "input.mp4"
    download(source, mp4)

    if not mp4.exists() or mp4.stat().st_size == 0:
        raise RuntimeError("A letöltött MP4 hiányzik vagy üres.")

    media = probe(mp4)

    print(
        f"  FAST REMUX | video={media['video']} "
        f"audio={media['audio'] or 'nincs'} "
        f"duration={media['duration']:.1f}s",
        flush=True,
    )

    if media["video"] != "h264":
        raise RuntimeError(
            f"Nem H.264 video ({media['video']}); "
            "nem transzkódoljuk, ezért SKIP."
        )

    if media["audio"] not in ("aac", ""):
        raise RuntimeError(
            f"Nem AAC audio ({media['audio']}); "
            "nem transzkódoljuk, ezért SKIP."
        )

    playlist = outdir / "index.m3u8"

    run([
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i", str(mp4),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "copy",
        "-c:a", "copy",
        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_playlist_type", "vod",
        "-hls_segment_type", "mpegts",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename",
        str(outdir / "seg_%05d.ts"),
        str(playlist),
    ])

    refs = parse_hls_playlist(
        playlist.read_text(
            encoding="utf-8",
            errors="replace",
        )
    )

    if not refs:
        raise RuntimeError("Nem készült HLS playlist.")

    local = {
        p.name: p
        for p in outdir.glob("seg_*.ts")
    }

    missing = [
        filename
        for filename, _duration in refs
        if filename not in local
    ]

    if missing:
        raise RuntimeError(
            f"Hiányzó szegmens: {missing[:5]}"
        )

    upload_files = [
        (
            local[filename],
            f"{ROOT.as_posix()}/{key}/{filename}",
        )
        for filename, _duration in refs
    ]

    print(
        f"  Feltöltés: {len(upload_files)} szegmens + playlist",
        flush=True,
    )

    # One HF commit per clip, not one commit per segment.
    commit_files(
        upload_files + [
            (
                playlist,
                f"{ROOT.as_posix()}/{key}/index.m3u8",
            )
        ],
        f"KLIPEK TV: remux {Path(source).stem}",
    )

    return {
        "source": source,
        "title": Path(source).stem,
        "duration": round(sum(d for _, d in refs), 3),
        "segments": [
            {
                "duration": round(d, 3),
                "path": f"{ROOT.as_posix()}/{key}/{filename}",
            }
            for filename, d in refs
        ],
    }


def main():
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg hiányzik.")

    if not shutil.which("ffprobe"):
        raise SystemExit("ffprobe hiányzik.")

    print("Remote fájllista ellenőrzése...", flush=True)
    repo_files = list_repo_files()

    sources = list_mp4s(repo_files)

    print(f"Talált MP4-ek: {len(sources)}", flush=True)

    if not sources:
        raise SystemExit("Nem találtam MP4 fájlt.")

    manifest_clips = []
    pending = []

    for n, source in enumerate(sources, 1):
        existing = remote_clip_item(source, repo_files)

        if existing:
            manifest_clips.append(existing)
            print(
                f"[{n}/{len(sources)}] KÉSZ -> SKIP: {source}",
                flush=True,
            )
        else:
            pending.append((n, source))

    print(
        f"\nMár kész: {len(manifest_clips)} | "
        f"FAST REMUX: {len(pending)}",
        flush=True,
    )

    with tempfile.TemporaryDirectory(
        prefix="klippek-tv-fast-"
    ) as td:
        work = Path(td)

        for n, source in pending:
            print(
                f"\n[{n}/{len(sources)}] FAST REMUX: {source}",
                flush=True,
            )

            try:
                item = build_one(
                    source,
                    work / f"clip_{n}",
                    ROOT,
                )
                manifest_clips.append(item)

            except Exception as e:
                print(
                    f"SKIP: {source}: {e}",
                    file=sys.stderr,
                    flush=True,
                )

        if not manifest_clips:
            raise SystemExit(
                "Egyetlen klipet sem sikerült feldolgozni."
            )

        manifest = {
            "version": 2,
            "mode": "fast-remux",
            "generatedAt": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(),
            ),
            "segmentSeconds": SEGMENT_SECONDS,
            "clips": manifest_clips,
        }

        manifest_path = work / "manifest.json"

        manifest_path.write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

        commit_files(
            [
                (
                    manifest_path,
                    f"{ROOT.as_posix()}/manifest.json",
                )
            ],
            "KLIPEK TV: update fast-remux manifest",
        )

    total = sum(x["duration"] for x in manifest_clips)

    print("\n================================", flush=True)
    print("KLIPEK TV FAST REMUX KÉSZ", flush=True)
    print(f"Clips: {len(manifest_clips)}", flush=True)
    print(f"Total hours: {total / 3600:.2f}", flush=True)
    print("================================", flush=True)


if __name__ == "__main__":
    main()
