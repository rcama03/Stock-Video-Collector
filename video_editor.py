#!/usr/bin/env python3
"""
Video Editor: Replace irrelevant clips with relevant stock footage.

Pipeline:
  1. Detect scene/clip boundaries in the input video  (ffmpeg scene filter)
  2. Transcribe the voiceover with timestamps          (OpenAI Whisper)
  3. Score each clip's visual relevance to its speech  (OpenAI CLIP)
  4. For irrelevant clips, search stock video APIs:
       • Pexels   (free, 200 req/h  — set PEXELS_API_KEY or --pexels-key)
       • Pixabay  (free, unlimited  — set PIXABAY_API_KEY or --pixabay-key)
  5. Download the best match and trim/loop to the original clip duration
  6. Re-assemble the video, keeping the original voiceover audio

Usage:
    python video_editor.py input.mp4 [output.mp4] [options]

    --pexels-key  KEY   Pexels API key  (or env PEXELS_API_KEY)
    --pixabay-key KEY   Pixabay API key (or env PIXABAY_API_KEY)

    Get free keys at:
      https://www.pexels.com/api/
      https://pixabay.com/api/docs/

    --scene-threshold FLOAT   Scene change sensitivity 0–1 (default 0.3)
    --relevance-threshold FLOAT  CLIP cutoff (default 0.20, lower = stricter)
    --whisper-model STR       tiny/base/small/medium/large (default base)
    --dry-run                 Report what would change without writing output
    -q / --quiet              Suppress progress output
"""

import argparse
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

DEFAULT_DB = Path.home() / ".artlist_scraper" / "artlist_results.db"

STOPWORDS = {
    "a","an","the","is","it","in","on","at","to","of","and","or","for",
    "with","this","that","are","was","were","has","have","had","i","you",
    "we","they","he","she","be","as","by","from","but","not","so","if",
    "its","all","can","will","just","do","did","get","got","our","your",
    "there","their","them","these","those","some","any","more","then","when",
    "which","who","what","how","about","would","could","should","also","very",
    "now","here","see","my","me","him","her","us","up","out","into","over",
    "after","while","where","been","no","yes","like","um","uh","ah","okay",
    "right","so","well","actually","basically","really","going","want","need",
}


# ──────────────────────────────────────────────────────────────────────────────
# Dependency bootstrap
# ──────────────────────────────────────────────────────────────────────────────

def ensure_deps() -> None:
    import importlib.util
    pkgs = {
        "openai-whisper": "whisper",
        "transformers":   "transformers",
        "Pillow":         "PIL",
        "torch":          "torch",
    }
    missing = [pkg for pkg, mod in pkgs.items()
               if importlib.util.find_spec(mod) is None]
    if missing:
        print(f"[setup] Installing: {', '.join(missing)}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q"] + missing)


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
    """Return [(start_sec, end_sec), ...] for each detected scene."""
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
    """Frame-accurate cut with re-encode."""
    subprocess.check_call([
        "ffmpeg", "-y",
        "-ss", str(start), "-t", str(duration),
        "-i", video,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-an", out,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fit_clip(inp: str, out: str, width: int, height: int,
             fps: float, duration: float) -> None:
    """Transcode to target spec, looping if source is shorter than duration."""
    src_dur = video_info(inp)["duration"]
    loops = max(0, math.ceil(duration / src_dur) - 1) if src_dur < duration else 0
    subprocess.check_call([
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
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


def download_url(url: str, out_path: str, label: str = "") -> bool:
    """Download a video URL using ffmpeg (handles HLS and direct MP4)."""
    cmd = [
        "ffmpeg", "-y",
        "-headers", "User-Agent: Mozilla/5.0\r\n",
        "-i", url,
        "-c", "copy",
        out_path,
    ]
    try:
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return Path(out_path).stat().st_size > 1024
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Transcription
# ──────────────────────────────────────────────────────────────────────────────

def transcribe(audio_wav: str, model_name: str = "base") -> list:
    import whisper
    model = whisper.load_model(model_name)
    return model.transcribe(audio_wav)["segments"]


def scene_text(segments: list, start: float, end: float) -> str:
    return " ".join(
        s["text"].strip()
        for s in segments
        if s["start"] < end and s["end"] > start
    )


# ──────────────────────────────────────────────────────────────────────────────
# CLIP visual relevance
# ──────────────────────────────────────────────────────────────────────────────

_clip_model = _clip_proc = None


def _load_clip():
    global _clip_model, _clip_proc
    if _clip_model is None:
        from transformers import CLIPModel, CLIPProcessor
        print("[clip] Loading CLIP model…")
        _clip_model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
        _clip_proc  = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
    return _clip_model, _clip_proc


def clip_relevance(frame_jpg: str, text: str) -> float:
    """Cosine similarity between a frame and text, normalized to [0, 1]."""
    import torch
    from PIL import Image
    model, proc = _load_clip()
    image = Image.open(frame_jpg).convert("RGB")
    inputs = proc(text=[text], images=image, return_tensors="pt",
                  padding=True, truncation=True, max_length=77)
    with torch.no_grad():
        out  = model(**inputs)
        ie   = out.image_embeds / out.image_embeds.norm(dim=-1, keepdim=True)
        te   = out.text_embeds  / out.text_embeds.norm(dim=-1, keepdim=True)
        sim  = (ie * te).sum().item()
    return (sim + 1.0) / 2.0


# ──────────────────────────────────────────────────────────────────────────────
# Keyword helpers
# ──────────────────────────────────────────────────────────────────────────────

def tokenize(text: str) -> set:
    tokens = re.sub(r"[^\w\s]", " ", text.lower()).split()
    return {t for t in tokens if t and t not in STOPWORDS}


def best_keywords(text: str, n: int = 5) -> str:
    """Return the top N content words from transcript text as a search query."""
    tokens = list(tokenize(text))
    # Prefer longer (more specific) words
    tokens.sort(key=len, reverse=True)
    return " ".join(tokens[:n])


# ──────────────────────────────────────────────────────────────────────────────
# Stock video API search
# ──────────────────────────────────────────────────────────────────────────────

def _http_get(url: str, headers: dict = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def search_pexels(query: str, duration: float, api_key: str) -> Optional[str]:
    """
    Search Pexels Videos API for a clip matching query and duration.
    Returns a direct MP4 download URL or None.
    Free API key: https://www.pexels.com/api/
    """
    params = urllib.parse.urlencode({
        "query":        query,
        "per_page":     15,
        "min_duration": max(1, int(duration * 0.6)),
        "max_duration": max(5, int(duration * 1.6)),
        "orientation":  "landscape",
        "size":         "medium",
    })
    try:
        data = _http_get(
            f"https://api.pexels.com/videos/search?{params}",
            headers={"Authorization": api_key},
        )
    except Exception as e:
        print(f"    [pexels] API error: {e}")
        return None

    videos = data.get("videos", [])
    if not videos:
        return None

    # Pick the video whose duration is closest to target
    best = min(videos, key=lambda v: abs(v.get("duration", 0) - duration))

    # Prefer HD MP4, then SD MP4
    files = best.get("video_files", [])
    mp4s  = [f for f in files
             if f.get("file_type") == "video/mp4" and f.get("link")]
    mp4s.sort(key=lambda f: (
        f.get("quality") not in ("hd",),   # hd first
        -f.get("width", 0)
    ))

    return mp4s[0]["link"] if mp4s else None


def search_pixabay(query: str, duration: float, api_key: str) -> Optional[str]:
    """
    Search Pixabay Videos API for a clip matching query and duration.
    Returns a direct MP4 download URL or None.
    Free API key: https://pixabay.com/api/docs/
    """
    params = urllib.parse.urlencode({
        "key":          api_key,
        "q":            query,
        "min_duration": max(1, int(duration * 0.6)),
        "max_duration": max(5, int(duration * 1.6)),
        "video_type":   "film",
        "per_page":     15,
        "order":        "relevant",
    })
    try:
        data = _http_get(f"https://pixabay.com/api/videos/?{params}")
    except Exception as e:
        print(f"    [pixabay] API error: {e}")
        return None

    hits = data.get("hits", [])
    if not hits:
        return None

    best = min(hits, key=lambda v: abs(v.get("duration", 0) - duration))
    videos = best.get("videos", {})

    for quality in ("large", "medium", "small", "tiny"):
        v = videos.get(quality, {})
        if v.get("url"):
            return v["url"]
    return None


def find_stock_clip(query: str, duration: float,
                    pexels_key: str, pixabay_key: str,
                    tmpdir: Path) -> Optional[str]:
    """
    Search Pexels then Pixabay for a clip matching query+duration.
    Downloads the clip to tmpdir and returns its local path, or None.
    """
    keywords = best_keywords(query)
    if not keywords:
        return None

    sources = []
    if pexels_key:
        sources.append(("Pexels",   lambda: search_pexels(keywords, duration, pexels_key)))
    if pixabay_key:
        sources.append(("Pixabay",  lambda: search_pixabay(keywords, duration, pixabay_key)))

    if not sources:
        return None

    for name, search_fn in sources:
        print(f"    [{name.lower()}] Searching: \"{keywords}\"")
        url = search_fn()
        if not url:
            print(f"    [{name.lower()}] No results")
            continue

        out = str(tmpdir / f"stock_{abs(hash(url)) % 10**8}.mp4")
        print(f"    [{name.lower()}] Downloading…")
        if download_url(url, out):
            print(f"    [{name.lower()}] Downloaded OK")
            return out
        else:
            print(f"    [{name.lower()}] Download failed, trying next source…")

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Local library fallback (optional, searched last)
# ──────────────────────────────────────────────────────────────────────────────

def search_local_library(query: str, duration: float,
                         db_path: str, tol: float = 0.4) -> Optional[str]:
    db = Path(db_path)
    if not db.exists():
        return None
    lo, hi = duration * (1 - tol), duration * (1 + tol)
    kw = list(tokenize(query))[:6]
    if not kw:
        return None

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = []

    try:
        fts_q = " OR ".join(kw)
        cur.execute("""
            SELECT c.file_path, c.title FROM clips c
            JOIN clips_fts f ON c.id = f.rowid
            WHERE clips_fts MATCH ?
              AND c.file_path IS NOT NULL AND c.file_path != ''
              AND (c.duration IS NULL OR c.duration BETWEEN ? AND ?)
            ORDER BY bm25(clips_fts) LIMIT 5
        """, (fts_q, lo, hi))
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        pass

    conn.close()

    for row in rows:
        fp = row["file_path"]
        if fp and Path(fp).exists():
            return fp
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ──────────────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    verbose     = not args.quiet
    pexels_key  = args.pexels_key  or os.environ.get("PEXELS_API_KEY",  "")
    pixabay_key = args.pixabay_key or os.environ.get("PIXABAY_API_KEY", "")

    def log(msg: str) -> None:
        if verbose:
            print(msg)

    inp = Path(args.input).resolve()
    if not inp.exists():
        sys.exit(f"Error: input file not found: {inp}")

    out = Path(args.output).resolve() if args.output \
        else inp.with_stem(inp.stem + "_edited")

    if not pexels_key and not pixabay_key:
        print(
            "\n[warning] No stock API keys provided.\n"
            "  Set PEXELS_API_KEY and/or PIXABAY_API_KEY env vars, or use\n"
            "  --pexels-key / --pixabay-key.\n"
            "  Only local library will be searched.\n"
            "  Free keys: https://www.pexels.com/api/ | https://pixabay.com/api/docs/\n"
        )

    ensure_deps()

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
        log(f"\n[2/4] Transcribing voiceover (model={args.whisper_model})…")
        wav = str(tmpdir / "audio.wav")
        extract_audio(str(inp), wav)
        segments = transcribe(wav, args.whisper_model)
        log(f"      {len(segments)} speech segment(s)")

        # ── 3. Per-scene analysis ──────────────────────────────────────────
        log("\n[3/4] Analysing scenes with CLIP…")
        clip_files: list = []
        n_replaced = 0
        report: list = []

        for i, (start, end) in enumerate(scenes):
            dur    = end - start
            spoken = scene_text(segments, start, end).strip()

            log(f"\n  Scene {i+1}/{len(scenes)}: {start:.2f}s–{end:.2f}s ({dur:.2f}s)")
            if spoken:
                preview = spoken[:90] + ("…" if len(spoken) > 90 else "")
                log(f"    Speech: \"{preview}\"")
            else:
                log("    Speech: (none — keeping as B-roll)")

            entry = {
                "index":       i + 1,
                "start":       round(start, 2),
                "end":         round(end, 2),
                "duration":    round(dur, 2),
                "spoken":      spoken,
                "action":      "keep",
                "replacement": None,
            }

            # Extract the original clip
            orig = str(tmpdir / f"orig_{i:04d}.mp4")
            cut_clip(str(inp), start, dur, orig)

            if args.dry_run:
                entry["action"] = "dry-run"
                report.append(entry)
                continue

            # No speech → keep as B-roll
            if not spoken:
                _add_fitted(orig, tmpdir, i, W, H, FPS, dur, clip_files)
                entry["action"] = "keep-broll"
                report.append(entry)
                continue

            # CLIP relevance check
            frame = str(tmpdir / f"frame_{i:04d}.jpg")
            extract_frame(orig, dur / 2, frame)
            score = clip_relevance(frame, spoken)
            log(f"    CLIP relevance: {score:.3f} (threshold {args.relevance_threshold})")

            if score >= args.relevance_threshold:
                log("    → Relevant — keeping original")
                _add_fitted(orig, tmpdir, i, W, H, FPS, dur, clip_files)
                entry["action"] = "keep"
                report.append(entry)
                continue

            # ── Low relevance: search for replacement ──────────────────────
            log(f"    → Irrelevant (score {score:.3f}) — searching stock libraries…")

            replacement_path: Optional[str] = None
            replacement_label: str = ""

            # 1. Try online stock APIs (Pexels, Pixabay)
            replacement_path = find_stock_clip(
                spoken, dur, pexels_key, pixabay_key, tmpdir
            )
            if replacement_path:
                replacement_label = "stock (online)"

            # 2. Fall back to local library
            if not replacement_path:
                log("    [local] Searching local library…")
                replacement_path = search_local_library(spoken, dur, args.db)
                if replacement_path:
                    replacement_label = f"local: {Path(replacement_path).name}"
                    log(f"    [local] Found: {replacement_label}")

            if replacement_path:
                fitted = str(tmpdir / f"fitted_{i:04d}.mp4")
                fit_clip(replacement_path, fitted, W, H, FPS, dur)
                clip_files.append(fitted)
                n_replaced += 1
                entry["action"]      = "replaced"
                entry["replacement"] = replacement_label
            else:
                log("    → No replacement found — keeping original")
                _add_fitted(orig, tmpdir, i, W, H, FPS, dur, clip_files)
                entry["action"] = "keep-no-match"

            report.append(entry)

        # ── 4. Assemble ────────────────────────────────────────────────────
        _print_report(report, verbose)

        if args.dry_run:
            log("\n[editor] Dry-run complete — no output file written.")
            return

        log(f"\n[4/4] Assembling output ({n_replaced}/{len(scenes)} clip(s) replaced)…")
        concat_mp4 = str(tmpdir / "concat.mp4")
        concat_clips(clip_files, concat_mp4)
        mux_audio(concat_mp4, wav, str(out))
        log(f"\n[editor] Done → {out}")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _add_fitted(orig: str, tmpdir: Path, i: int,
                W: int, H: int, FPS: float, dur: float,
                clip_files: list) -> None:
    fitted = str(tmpdir / f"fitted_{i:04d}.mp4")
    fit_clip(orig, fitted, W, H, FPS, dur)
    clip_files.append(fitted)


def _print_report(report: list, verbose: bool) -> None:
    if not verbose:
        return
    print("\n── Scene Report ─────────────────────────────────────────────────────────")
    for s in report:
        label = {
            "keep":         "kept (relevant)",
            "keep-broll":   "kept (B-roll / no speech)",
            "keep-no-match":"kept (no replacement found)",
            "replaced":     f"REPLACED → {s['replacement']}",
            "dry-run":      "dry-run",
        }.get(s["action"], s["action"])
        t = f"  Scene {s['index']:>2}: {s['start']:>7.2f}s–{s['end']:>7.2f}s ({s['duration']:>5.2f}s)  {label}"
        print(t)
        if s["spoken"]:
            preview = s["spoken"][:80] + ("…" if len(s["spoken"]) > 80 else "")
            print(f"           \"{preview}\"")
    print("─────────────────────────────────────────────────────────────────────────\n")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replace irrelevant video clips with relevant stock footage.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input",  help="Input video file (with voiceover)")
    parser.add_argument("output", nargs="?",
                        help="Output path (default: <input>_edited.mp4)")

    api = parser.add_argument_group("Stock API keys")
    api.add_argument("--pexels-key",  default="",
                     help="Pexels API key (or set PEXELS_API_KEY env var)")
    api.add_argument("--pixabay-key", default="",
                     help="Pixabay API key (or set PIXABAY_API_KEY env var)")

    tuning = parser.add_argument_group("Tuning")
    tuning.add_argument("--scene-threshold",    type=float, default=0.3,
                        help="Scene change sensitivity 0–1 (lower = more splits)")
    tuning.add_argument("--relevance-threshold", type=float, default=0.20,
                        help="CLIP cosine similarity below which clip is replaced")
    tuning.add_argument("--whisper-model", default="base",
                        choices=["tiny","base","small","medium","large"],
                        help="Whisper model size (larger = more accurate)")

    misc = parser.add_argument_group("Misc")
    misc.add_argument("--db", default=str(DEFAULT_DB),
                      help="Local stock-video SQLite database (fallback source)")
    misc.add_argument("--dry-run",  action="store_true",
                      help="Report what would change without writing output")
    misc.add_argument("-q", "--quiet", action="store_true",
                      help="Suppress progress output")

    run(parser.parse_args())


if __name__ == "__main__":
    main()
