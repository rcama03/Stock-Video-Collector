#!/usr/bin/env python3
"""
Video Editor: Automatically replace irrelevant clips with relevant stock footage.

Analyzes a video's voiceover using Whisper, detects scene boundaries, scores each
clip's visual relevance to its spoken content using CLIP, and replaces low-relevance
clips with matching clips from your local stock video library.

Usage:
    python video_editor.py input.mp4 [output.mp4] [options]

Options:
    --db PATH                Path to stock video DB
                             (default: ~/.artlist_scraper/artlist_results.db)
    --scene-threshold FLOAT  Scene change sensitivity 0.0–1.0 (default: 0.3,
                             lower = more scene splits)
    --relevance-threshold FLOAT  CLIP cosine similarity cutoff below which a
                             clip is considered irrelevant (default: 0.20)
    --whisper-model STR      Whisper model: tiny/base/small/medium/large
                             (default: base)
    --no-clip                Skip CLIP visual analysis — produce a scene/
                             transcript report only, no replacements
    --dry-run                Report what would be replaced without writing output
    -q / --quiet             Suppress progress output
"""

import sys
import os
import json
import math
import re
import sqlite3
import shutil
import subprocess
import tempfile
import argparse
from pathlib import Path
from typing import Optional

DEFAULT_DB = Path.home() / ".artlist_scraper" / "artlist_results.db"

STOPWORDS = {
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "of", "and", "or",
    "for", "with", "this", "that", "are", "was", "were", "has", "have", "had",
    "i", "you", "we", "they", "he", "she", "be", "as", "by", "from", "but",
    "not", "so", "if", "its", "all", "can", "will", "just", "do", "did", "get",
    "got", "our", "your", "there", "their", "them", "these", "those", "some",
    "any", "more", "then", "when", "which", "who", "what", "how", "about",
    "would", "could", "should", "also", "very", "now", "here", "see", "my",
    "me", "him", "her", "us", "up", "out", "into", "over", "after", "while",
    "where", "been", "no", "yes", "ok", "like", "um", "uh", "ah",
}


# ──────────────────────────────────────────────────────────────────────────────
# Dependency bootstrap
# ──────────────────────────────────────────────────────────────────────────────

def ensure_deps(use_clip: bool) -> None:
    import importlib

    pkgs = {"openai-whisper": "whisper"}
    if use_clip:
        pkgs["transformers"] = "transformers"
        pkgs["Pillow"] = "PIL"
        pkgs["torch"] = "torch"

    missing = [pkg for pkg, mod in pkgs.items()
               if not _can_import(mod)]

    if missing:
        print(f"[setup] Installing: {', '.join(missing)}")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q"] + missing
        )


def _can_import(module: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(module) is not None


# ──────────────────────────────────────────────────────────────────────────────
# FFmpeg / FFprobe helpers
# ──────────────────────────────────────────────────────────────────────────────

def _ffprobe(path: str) -> dict:
    out = subprocess.check_output([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", path,
    ])
    return json.loads(out)


def video_info(path: str) -> dict:
    """Return {duration, width, height, fps} for a video file."""
    data = _ffprobe(path)
    fmt = data.get("format", {})
    vs = next((s for s in data.get("streams", [])
                if s["codec_type"] == "video"), {})

    num, den = (vs.get("r_frame_rate", "30/1")).split("/")
    fps = int(num) / max(int(den), 1)

    return {
        "duration": float(fmt.get("duration", 0)),
        "width":    int(vs.get("width", 1920)),
        "height":   int(vs.get("height", 1080)),
        "fps":      fps,
    }


def detect_scenes(path: str, threshold: float = 0.3) -> list:
    """
    Return [(start_sec, end_sec), ...] for each detected scene.
    Uses ffmpeg's scene-change detection filter.
    """
    duration = video_info(path)["duration"]

    cmd = [
        "ffmpeg", "-i", path,
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-vsync", "vfr", "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    times = [0.0]
    seen: set = set()
    for line in result.stderr.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = round(float(m.group(1)), 3)
            if t not in seen and t > times[-1] + 0.5:
                times.append(t)
                seen.add(t)

    times.append(duration)
    return [(times[i], times[i + 1]) for i in range(len(times) - 1)]


def extract_audio(video: str, out_wav: str) -> None:
    subprocess.check_call([
        "ffmpeg", "-y", "-i", video,
        "-vn", "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le", out_wav,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def extract_frame(video: str, time_sec: float, out_jpg: str) -> None:
    subprocess.check_call([
        "ffmpeg", "-y", "-ss", str(time_sec), "-i", video,
        "-frames:v", "1", "-q:v", "2", out_jpg,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cut_clip(video: str, start: float, duration: float, out: str) -> None:
    """Accurate cut using re-encode for frame precision."""
    subprocess.check_call([
        "ffmpeg", "-y",
        "-ss", str(start), "-t", str(duration),
        "-i", video,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-an", out,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fit_clip(inp: str, out: str, width: int, height: int,
             fps: float, duration: float) -> None:
    """
    Transcode a clip to the target spec (size, fps, duration).
    Loops the source if it is shorter than the target duration.
    """
    src_dur = video_info(inp)["duration"]
    loops = max(0, math.ceil(duration / src_dur) - 1) if src_dur < duration else 0

    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", str(loops),
        "-i", inp,
        "-t", str(duration),
        "-vf", (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={fps}"
        ),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-an", out,
    ]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def concat_clips(clip_paths: list, out: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in clip_paths:
            f.write(f"file '{p}'\n")
        lst = f.name
    subprocess.check_call([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", lst, "-c", "copy", out,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.unlink(lst)


def mux_audio(video: str, audio_wav: str, out: str) -> None:
    subprocess.check_call([
        "ffmpeg", "-y",
        "-i", video, "-i", audio_wav,
        "-c:v", "copy", "-c:a", "aac",
        "-map", "0:v:0", "-map", "1:a:0",
        "-shortest", out,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ──────────────────────────────────────────────────────────────────────────────
# Transcription (Whisper)
# ──────────────────────────────────────────────────────────────────────────────

def transcribe(audio_wav: str, model_name: str = "base") -> list:
    """Return Whisper segments: [{start, end, text}, ...]."""
    import whisper
    model = whisper.load_model(model_name)
    result = model.transcribe(audio_wav)
    return result["segments"]


def scene_text(segments: list, start: float, end: float) -> str:
    """Collect transcript text that overlaps with [start, end]."""
    return " ".join(
        s["text"].strip()
        for s in segments
        if s["start"] < end and s["end"] > start
    )


# ──────────────────────────────────────────────────────────────────────────────
# CLIP visual relevance
# ──────────────────────────────────────────────────────────────────────────────

_clip_model = None
_clip_proc = None


def _load_clip():
    global _clip_model, _clip_proc
    if _clip_model is None:
        from transformers import CLIPModel, CLIPProcessor
        print("[clip] Loading CLIP model (one-time download ~600 MB)…")
        _clip_model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
        _clip_proc  = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
    return _clip_model, _clip_proc


def clip_relevance(frame_jpg: str, text: str) -> float:
    """
    Cosine similarity between a video frame and a text description.
    Returns a value in [0, 1] where higher = more relevant.
    """
    import torch
    from PIL import Image

    model, proc = _load_clip()
    image = Image.open(frame_jpg).convert("RGB")

    inputs = proc(
        text=[text], images=image,
        return_tensors="pt", padding=True,
        truncation=True, max_length=77,
    )
    with torch.no_grad():
        outputs  = model(**inputs)
        img_emb  = outputs.image_embeds
        text_emb = outputs.text_embeds
        img_emb  = img_emb  / img_emb.norm(dim=-1, keepdim=True)
        text_emb = text_emb / text_emb.norm(dim=-1, keepdim=True)
        sim = (img_emb * text_emb).sum().item()

    # cosine similarity in [-1, 1] → normalize to [0, 1]
    return (sim + 1.0) / 2.0


# ──────────────────────────────────────────────────────────────────────────────
# Keyword matching helpers
# ──────────────────────────────────────────────────────────────────────────────

def tokenize(text: str) -> set:
    tokens = re.sub(r"[^\w\s]", " ", text.lower()).split()
    return {t for t in tokens if t and t not in STOPWORDS}


def keyword_score(desc: str, query: str) -> float:
    a, b = tokenize(desc), tokenize(query)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ──────────────────────────────────────────────────────────────────────────────
# Local library search
# ──────────────────────────────────────────────────────────────────────────────

def search_library(query: str, target_dur: float,
                   db_path: str, tolerance: float = 0.4) -> Optional[dict]:
    """
    Find the best matching clip in the local SQLite library.
    Returns a row dict with at least {title, file_path, duration} or None.
    """
    db = Path(db_path)
    if not db.exists():
        return None

    lo = target_dur * (1.0 - tolerance)
    hi = target_dur * (1.0 + tolerance)
    kw = list(tokenize(query))[:6]

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = []

    # ── Try FTS5 full-text search first
    try:
        if kw:
            fts_q = " OR ".join(kw)
            cur.execute("""
                SELECT c.id, c.title, c.file_path, c.duration, c.tags, c.collection
                FROM clips c
                JOIN clips_fts f ON c.id = f.rowid
                WHERE clips_fts MATCH ?
                  AND c.file_path IS NOT NULL AND c.file_path != ''
                  AND (c.duration IS NULL OR c.duration BETWEEN ? AND ?)
                ORDER BY bm25(clips_fts)
                LIMIT 30
            """, (fts_q, lo, hi))
            rows = cur.fetchall()
    except sqlite3.OperationalError:
        pass

    # ── Fallback: LIKE search
    if not rows and kw:
        clauses = " OR ".join(
            "(c.title LIKE ? OR c.tags LIKE ?)" for _ in kw
        )
        params = [v for t in kw for v in (f"%{t}%", f"%{t}%")]
        params += [lo, hi]
        cur.execute(f"""
            SELECT c.id, c.title, c.file_path, c.duration, c.tags, c.collection
            FROM clips c
            WHERE ({clauses})
              AND c.file_path IS NOT NULL AND c.file_path != ''
              AND (c.duration IS NULL OR c.duration BETWEEN ? AND ?)
            LIMIT 30
        """, params)
        rows = cur.fetchall()

    conn.close()

    if not rows:
        return None

    best = max(
        rows,
        key=lambda r: keyword_score(
            f"{r['title'] or ''} {r['tags'] or ''} {r['collection'] or ''}",
            query,
        ),
    )
    fp = best["file_path"]
    if fp and Path(fp).exists():
        return dict(best)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ──────────────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    verbose = not args.quiet
    use_clip = not args.no_clip

    def log(msg: str, indent: int = 0) -> None:
        if verbose:
            print("  " * indent + msg)

    inp = Path(args.input).resolve()
    if not inp.exists():
        sys.exit(f"Error: input file not found: {inp}")

    out = Path(args.output).resolve() if args.output \
        else inp.with_stem(inp.stem + "_edited")

    ensure_deps(use_clip=use_clip)

    info = video_info(str(inp))
    W, H, FPS = info["width"], info["height"], info["fps"]
    log(f"\n[editor] Input : {inp.name}")
    log(f"[editor] Output: {out.name}")
    log(f"[editor] Spec  : {W}×{H} @ {FPS:.2f} fps, {info['duration']:.1f}s")

    tmpdir = Path(tempfile.mkdtemp(prefix="ve_"))
    try:
        # ── 1. Scene detection ─────────────────────────────────────────────
        log(f"\n[1/4] Detecting scenes (threshold={args.scene_threshold})…")
        scenes = detect_scenes(str(inp), args.scene_threshold)
        log(f"      {len(scenes)} scene(s) found")

        # ── 2. Transcription ───────────────────────────────────────────────
        log(f"\n[2/4] Transcribing audio (model={args.whisper_model})…")
        wav = str(tmpdir / "audio.wav")
        extract_audio(str(inp), wav)
        segments = transcribe(wav, args.whisper_model)
        log(f"      {len(segments)} speech segment(s)")

        # ── 3. Per-scene relevance + replacement ───────────────────────────
        log(f"\n[3/4] Analysing scenes (CLIP={'on' if use_clip else 'off'})…")
        clip_files: list = []
        n_replaced = 0
        report: list = []

        for i, (start, end) in enumerate(scenes):
            dur = end - start
            spoken = scene_text(segments, start, end).strip()

            log(f"\n  Scene {i+1}/{len(scenes)}: {start:.2f}s–{end:.2f}s ({dur:.2f}s)")
            log(f"    Speech: \"{spoken[:100]}{'…' if len(spoken) > 100 else ''}\"" if spoken
                else "    Speech: (none — B-roll, keeping)", 0)

            scene_entry = {
                "index": i + 1,
                "start": round(start, 3),
                "end":   round(end, 3),
                "duration": round(dur, 3),
                "spoken": spoken,
                "action": "keep",
                "replacement": None,
            }

            # Extract scene clip
            orig = str(tmpdir / f"orig_{i:04d}.mp4")
            cut_clip(str(inp), start, dur, orig)

            if args.dry_run or args.no_clip:
                # In dry-run / no-clip mode just collect the report
                scene_entry["action"] = "report-only"
                report.append(scene_entry)
                continue

            # ── Compute CLIP relevance when there is speech
            if spoken and use_clip:
                frame = str(tmpdir / f"frame_{i:04d}.jpg")
                extract_frame(orig, dur / 2, frame)
                score = clip_relevance(frame, spoken)
                log(f"    CLIP relevance: {score:.3f} (threshold={args.relevance_threshold})")

                if score < args.relevance_threshold:
                    log(f"    → Irrelevant — searching library…")
                    rep = search_library(spoken, dur, args.db)

                    if rep:
                        log(f"    → Replacing with: \"{rep['title']}\"")
                        fitted = str(tmpdir / f"fitted_{i:04d}.mp4")
                        fit_clip(rep["file_path"], fitted, W, H, FPS, dur)
                        clip_files.append(fitted)
                        n_replaced += 1
                        scene_entry["action"] = "replaced"
                        scene_entry["replacement"] = rep["title"]
                    else:
                        log(f"    → No suitable replacement found — keeping original")
                        _keep(orig, tmpdir, i, W, H, FPS, dur, clip_files)
                        scene_entry["action"] = "kept-no-match"
                else:
                    log(f"    → Relevant — keeping original")
                    _keep(orig, tmpdir, i, W, H, FPS, dur, clip_files)
            else:
                log(f"    → No speech or CLIP disabled — keeping original")
                _keep(orig, tmpdir, i, W, H, FPS, dur, clip_files)

            report.append(scene_entry)

        # ── 4. Assemble final video ────────────────────────────────────────
        if args.dry_run or args.no_clip:
            _print_report(report, verbose)
            if args.dry_run:
                log("\n[editor] Dry-run complete — no output file written.")
            return

        log(f"\n[4/4] Assembling output ({n_replaced}/{len(scenes)} clip(s) replaced)…")
        concat_mp4 = str(tmpdir / "concat.mp4")
        concat_clips(clip_files, concat_mp4)
        mux_audio(concat_mp4, wav, str(out))

        _print_report(report, verbose)
        log(f"\n[editor] Done → {out}")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _keep(orig: str, tmpdir: Path, i: int,
          W: int, H: int, FPS: float, dur: float, clip_files: list) -> None:
    fitted = str(tmpdir / f"fitted_{i:04d}.mp4")
    fit_clip(orig, fitted, W, H, FPS, dur)
    clip_files.append(fitted)


def _print_report(report: list, verbose: bool) -> None:
    if not verbose:
        return
    print("\n── Scene Report ─────────────────────────────────────────────────")
    for s in report:
        action_str = {
            "keep":         "kept   ",
            "replaced":     f"REPLACED → {s['replacement']}",
            "kept-no-match":"kept (no match)",
            "report-only":  "report-only",
        }.get(s["action"], s["action"])
        print(f"  Scene {s['index']:>2}: {s['start']:>7.2f}s – {s['end']:>7.2f}s "
              f"({s['duration']:>5.2f}s)  {action_str}")
        if s["spoken"]:
            print(f"           \"{s['spoken'][:80]}{'…' if len(s['spoken'])>80 else ''}\"")
    print("─────────────────────────────────────────────────────────────────\n")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replace irrelevant video clips with relevant stock footage.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input",
                        help="Input video file (with voiceover)")
    parser.add_argument("output", nargs="?",
                        help="Output path (default: <input>_edited.mp4)")
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help="Stock video SQLite database")
    parser.add_argument("--scene-threshold", type=float, default=0.3,
                        help="Scene change sensitivity 0.0–1.0")
    parser.add_argument("--relevance-threshold", type=float, default=0.20,
                        help="CLIP cosine similarity cutoff (0.0–1.0)")
    parser.add_argument("--whisper-model", default="base",
                        choices=["tiny", "base", "small", "medium", "large"],
                        help="Whisper model size")
    parser.add_argument("--no-clip", action="store_true",
                        help="Skip CLIP visual analysis — report scenes only")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing output")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Suppress progress output")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
