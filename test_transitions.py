#!/usr/bin/env python3
"""
test_transitions.py — Tests xfade transitions + swoosh sound effects
Downloads 5 clips from Pexels, applies xfade between each, outputs ~25s test video.
Usage: python3 test_transitions.py [output.mp4]
"""

import os, sys, time, subprocess, requests, tempfile, shutil

FFMPEG   = "/usr/local/lib/python3.11/dist-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"
OUTPUT   = sys.argv[1] if len(sys.argv) > 1 else "/home/user/Stock-Video-Collector/test_transitions_out.mp4"
WORK     = "/tmp/test_trans"
RAWDIR   = f"{WORK}/raw"
SEGDIR   = f"{WORK}/seg"

os.makedirs(RAWDIR, exist_ok=True)
os.makedirs(SEGDIR, exist_ok=True)

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "api_keys.env"))
PEXELS_KEY = os.getenv("PEXELS_API_KEY")

# ── Config ────────────────────────────────────────────────────────────────────
CLIP_DURATION  = 5.0   # seconds per clip
XFADE_DURATION = 0.4   # seconds for crossfade overlap
SWOOSH_VOLUME  = 0.6   # relative volume of swoosh sfx

SEARCH_TERMS = [
    "airplane cockpit",
    "airport terminal",
    "plane landing",
    "business class cabin",
    "runway takeoff",
]

# Alternate xfade effects — one per transition
XFADE_EFFECTS = ["fade", "wipeleft", "slideleft", "fadeblack", "fade"]

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_dur(path):
    r = subprocess.run([FFMPEG,"-i",path], capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "Duration" in line:
            t = line.strip().split("Duration:")[1].split(",")[0].strip()
            h,m,s = t.split(":")
            return float(h)*3600 + float(m)*60 + float(s)
    return 0.0

def download(url, dst):
    try:
        r = requests.get(url, stream=True, timeout=30)
        if r.status_code != 200: return False
        with open(dst,"wb") as f:
            for chunk in r.iter_content(65536): f.write(chunk)
        return os.path.getsize(dst) > 10000
    except Exception as e:
        print(f"  Download error: {e}")
        return False

def fetch_pexels(query):
    """Fetch first suitable landscape video URL from Pexels."""
    headers = {"Authorization": PEXELS_KEY}
    try:
        r = requests.get("https://api.pexels.com/videos/search",
                         headers=headers,
                         params={"query":query,"per_page":5,"orientation":"landscape"},
                         timeout=20)
        if r.status_code != 200:
            print(f"  Pexels {r.status_code} for '{query}'")
            return None
        for v in r.json().get("videos", []):
            if v.get("duration", 0) < CLIP_DURATION + 2:
                continue
            for f in sorted(v.get("video_files",[]), key=lambda x: x.get("width",0), reverse=True):
                if 640 <= f.get("width",0) <= 1920:
                    return f["link"]
    except Exception as e:
        print(f"  Pexels error: {e}")
    return None

def make_seg(src, dst):
    """Trim to CLIP_DURATION, scale to 1280x720, no audio."""
    dur = get_dur(src)
    start = max(0, (dur - CLIP_DURATION) / 2)  # take middle portion
    cmd = [FFMPEG,"-y",
           "-ss", f"{start:.3f}",
           "-i", src,
           "-t", f"{CLIP_DURATION:.3f}",
           "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                  "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
           "-r","30","-c:v","libx264","-preset","fast","-crf","22",
           "-an", dst]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print(f"  make_seg error: {r.stderr.decode()[-200:]}")
    return r.returncode == 0

def make_swoosh(dst, duration=0.5):
    """Generate a short swoosh sound effect using ffmpeg aevalsrc."""
    # Frequency sweep 200Hz → 3000Hz
    expr = ("sin(2*PI*(200+2800*t/{d})*t)*exp(-3*t/{d})"
            .format(d=duration))
    cmd = [FFMPEG,"-y",
           "-f","lavfi",
           "-i", f"aevalsrc={expr}:s=44100:c=mono:d={duration}",
           "-c:a","aac","-b:a","128k", dst]
    r = subprocess.run(cmd, capture_output=True)
    return r.returncode == 0

# ── Step 1: Download clips ────────────────────────────────────────────────────
print("Step 1: Downloading clips from Pexels…")
segs = []
for i, term in enumerate(SEARCH_TERMS):
    print(f"  [{i+1}/{len(SEARCH_TERMS)}] '{term}'", end=" ")
    url = fetch_pexels(term)
    if not url:
        print("✗ no result")
        continue
    raw = f"{RAWDIR}/clip{i:02d}.mp4"
    seg = f"{SEGDIR}/seg{i:02d}.mp4"
    if not download(url, raw):
        print("✗ download failed")
        continue
    if not make_seg(raw, seg):
        print("✗ encode failed")
        continue
    segs.append(seg)
    print(f"✓  ({get_dur(seg):.1f}s)")
    time.sleep(0.3)

if len(segs) < 2:
    print("ERROR: Need at least 2 clips. Aborting.")
    sys.exit(1)

print(f"\n{len(segs)} clips ready.")

# ── Step 2: Build xfade chain ─────────────────────────────────────────────────
# xfade requires each clip to be trimmed so transitions line up correctly.
# For N clips with xfade overlap X seconds:
#   clip[i] starts at offset = i * (CLIP_DURATION - XFADE_DURATION)
# Total video duration = N * CLIP_DURATION - (N-1) * XFADE_DURATION

print("\nStep 2: Building xfade transition chain…")

# Build ffmpeg filter_complex for chained xfade
inputs = []
for seg in segs:
    inputs += ["-i", seg]

n = len(segs)
offset = CLIP_DURATION - XFADE_DURATION  # offset between clip starts

filter_parts = []
for i in range(n - 1):
    effect = XFADE_EFFECTS[i % len(XFADE_EFFECTS)]
    t = offset * (i + 1) - XFADE_DURATION  # xfade offset from start of chain
    if i == 0:
        in_a = f"[0:v]"
        in_b = f"[1:v]"
    else:
        in_a = f"[xf{i-1}]"
        in_b = f"[{i+1}:v]"
    out_label = f"[xf{i}]" if i < n - 2 else "[vout]"
    filter_parts.append(
        f"{in_a}{in_b}xfade=transition={effect}:duration={XFADE_DURATION}:offset={t:.3f}{out_label}"
    )

filter_complex = ";".join(filter_parts)

video_only = f"{WORK}/video_only.mp4"
cmd = [FFMPEG,"-y"] + inputs + [
    "-filter_complex", filter_complex,
    "-map", "[vout]",
    "-c:v","libx264","-preset","fast","-crf","21",
    "-r","30", video_only
]
r = subprocess.run(cmd, capture_output=True)
if r.returncode != 0:
    print("XFADE ERROR:", r.stderr.decode()[-600:])
    sys.exit(1)

vid_dur = get_dur(video_only)
print(f"  Video with xfade: {vid_dur:.1f}s ✓")

# ── Step 3: Generate swoosh SFX track ────────────────────────────────────────
print("\nStep 3: Generating swoosh SFX at each transition…")

swoosh_file = f"{WORK}/swoosh_single.aac"
make_swoosh(swoosh_file, duration=0.5)

# Build silence + swoosh audio track matching video duration
# One swoosh at each xfade transition point
transition_times = [offset * (i + 1) - XFADE_DURATION/2 for i in range(n - 1)]
print(f"  Transition timestamps: {[f'{t:.2f}s' for t in transition_times]}")

# Build amix of silence base + delayed swoosh instances
audio_inputs = ["-f","lavfi","-i",f"aevalsrc=0:s=44100:c=stereo:d={vid_dur:.3f}"]
audio_filter_parts = ["[0:a]anull[base]"]

for j, ts in enumerate(transition_times):
    audio_inputs += ["-i", swoosh_file]
    audio_filter_parts.append(
        f"[{j+1}:a]adelay={int(ts*1000)}|{int(ts*1000)}[sw{j}]"
    )

# Mix base silence + all swooshes
mix_inputs = "[base]" + "".join(f"[sw{j}]" for j in range(len(transition_times)))
audio_filter_parts.append(
    f"{mix_inputs}amix=inputs={1+len(transition_times)}:normalize=0,volume={SWOOSH_VOLUME}[aout]"
)

sfx_track = f"{WORK}/sfx_track.aac"
sfx_cmd = [FFMPEG,"-y"] + audio_inputs + [
    "-filter_complex", ";".join(audio_filter_parts),
    "-map","[aout]",
    "-c:a","aac","-b:a","128k","-t",f"{vid_dur:.3f}", sfx_track
]
r = subprocess.run(sfx_cmd, capture_output=True)
if r.returncode != 0:
    print("SFX ERROR:", r.stderr.decode()[-400:])
    print("  Skipping sfx, using silent track")
    sfx_track = None

# ── Step 4: Mux video + SFX ───────────────────────────────────────────────────
print("\nStep 4: Muxing video + swoosh SFX…")

if sfx_track and os.path.exists(sfx_track):
    mux_cmd = [FFMPEG,"-y",
               "-i", video_only,
               "-i", sfx_track,
               "-c:v","copy",
               "-c:a","aac","-b:a","128k",
               "-shortest", OUTPUT]
else:
    mux_cmd = [FFMPEG,"-y","-i", video_only,
               "-c:v","copy","-an", OUTPUT]

r = subprocess.run(mux_cmd, capture_output=True)
if r.returncode != 0:
    print("MUX ERROR:", r.stderr.decode()[-400:])
    sys.exit(1)

final_dur = get_dur(OUTPUT)
size = os.path.getsize(OUTPUT) / 1024 / 1024
print(f"\n✓  {OUTPUT}")
print(f"   Duration   : {final_dur:.1f}s")
print(f"   Clips used : {n}")
print(f"   Transitions: {n-1} xfade ({', '.join(XFADE_EFFECTS[:n-1])})")
print(f"   Size       : {size:.1f} MB")
print(f"\nWatch the output to check transitions are visible and swoosh sounds audible.")
