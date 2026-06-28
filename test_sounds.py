#!/usr/bin/env python3
"""
test_sounds.py — Compare 5 transition sound effects, all using slideleft xfade.
Generates one video per sound, then stacks them into a single comparison video.
Usage: python3 test_sounds.py
"""

import os, sys, time, subprocess, requests
from PIL import Image, ImageDraw, ImageFont

FFMPEG  = "/usr/local/lib/python3.11/dist-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"
OUTPUT  = "/home/user/Stock-Video-Collector/test_sounds_out.mp4"
WORK    = "/tmp/test_sounds"
RAWDIR  = f"{WORK}/raw"
SEGDIR  = f"{WORK}/seg"
SNDDIR  = f"{WORK}/snd"
OUTDIR  = f"{WORK}/out"

for d in [RAWDIR, SEGDIR, SNDDIR, OUTDIR]:
    os.makedirs(d, exist_ok=True)

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "api_keys.env"))
PEXELS_KEY = os.getenv("PEXELS_API_KEY")

CLIP_DURATION  = 4.0
XFADE_DURATION = 0.4
SEARCH_TERMS   = ["airplane cockpit", "airport lounge", "plane window view"]

# ── Sound definitions ─────────────────────────────────────────────────────────
# Each returns an aevalsrc expression + duration
SOUNDS = {
    "1_soft_whoosh": {
        "label": "1. Soft Whoosh",
        # Gentle sine sweep 300→1800Hz, soft envelope
        "expr": "sin(2*PI*(300+1500*t/0.6)*t)*0.5*exp(-2.5*t/0.6)",
        "dur": 0.6,
    },
    "2_bass_thud": {
        "label": "2. Bass Thud",
        # Low sine at 60Hz with fast decay + high harmonic click at start
        "expr": "sin(2*PI*60*t)*exp(-8*t/0.5) + 0.3*sin(2*PI*800*t)*exp(-30*t/0.5)",
        "dur": 0.5,
    },
    "3_tick_click": {
        "label": "3. Tick / Click",
        # Very short sharp high-frequency click
        "expr": "sin(2*PI*1200*t)*exp(-40*t/0.15)",
        "dur": 0.15,
    },
    "4_air_swoosh": {
        "label": "4. Air Swoosh",
        # Higher freq sweep 800→4000Hz, cinematic feel
        "expr": "sin(2*PI*(800+3200*t/0.5)*t)*0.45*exp(-4*t/0.5)",
        "dur": 0.5,
    },
    "5_combo": {
        "label": "5. Combo (Whoosh + Thud)",
        # Layered: soft whoosh + bass thud
        "expr": ("sin(2*PI*(300+1500*t/0.6)*t)*0.4*exp(-2.5*t/0.6)"
                 " + sin(2*PI*60*t)*0.5*exp(-8*t/0.6)"
                 " + 0.2*sin(2*PI*800*t)*exp(-30*t/0.6)"),
        "dur": 0.6,
    },
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_dur(path):
    r = subprocess.run([FFMPEG,"-i",path], capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "Duration" in line:
            t = line.strip().split("Duration:")[1].split(",")[0].strip()
            h,m,s = t.split(":")
            return float(h)*3600+float(m)*60+float(s)
    return 0.0

def download(url, dst):
    try:
        r = requests.get(url, stream=True, timeout=30)
        if r.status_code != 200: return False
        with open(dst,"wb") as f:
            for chunk in r.iter_content(65536): f.write(chunk)
        return os.path.getsize(dst) > 10000
    except: return False

def fetch_pexels(query):
    headers = {"Authorization": PEXELS_KEY}
    try:
        r = requests.get("https://api.pexels.com/videos/search",
                         headers=headers,
                         params={"query":query,"per_page":5,"orientation":"landscape"},
                         timeout=20)
        if r.status_code != 200: return None
        for v in r.json().get("videos",[]):
            if v.get("duration",0) < CLIP_DURATION+2: continue
            for f in sorted(v.get("video_files",[]),key=lambda x:x.get("width",0),reverse=True):
                if 640 <= f.get("width",0) <= 1920:
                    return f["link"]
    except: pass
    return None

def make_seg(src, dst):
    dur = get_dur(src)
    start = max(0,(dur-CLIP_DURATION)/2)
    cmd = [FFMPEG,"-y","-ss",f"{start:.3f}","-i",src,"-t",f"{CLIP_DURATION:.3f}",
           "-vf","scale=1280:720:force_original_aspect_ratio=decrease,"
                 "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
           "-r","30","-c:v","libx264","-preset","fast","-crf","22","-an",dst]
    return subprocess.run(cmd,capture_output=True).returncode == 0

def make_sound(key, dst):
    s = SOUNDS[key]
    cmd = [FFMPEG,"-y","-f","lavfi",
           "-i",f"aevalsrc={s['expr']}:s=44100:c=mono:d={s['dur']}",
           "-c:a","aac","-b:a","128k",dst]
    return subprocess.run(cmd,capture_output=True).returncode == 0

def add_label(seg, label, dst):
    """Overlay label PNG onto first 2s of video using ffmpeg overlay filter."""
    # Create label PNG with Pillow
    png = f"{WORK}/label_{os.path.basename(dst)}.png"
    img = Image.new("RGBA", (1280, 80), (0, 0, 0, 160))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 42)
    except:
        font = ImageFont.load_default()
    bbox = d.textbbox((0,0), label, font=font)
    tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
    d.text(((1280-tw)//2, (80-th)//2), label, font=font, fill="white")
    img.save(png)

    cmd = [FFMPEG,"-y","-i",seg,"-i",png,
           "-filter_complex","[0:v][1:v]overlay=0:20:format=auto[vout]",
           "-map","[vout]",
           "-c:v","libx264","-preset","fast","-crf","22","-an",dst]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print(f"\n  label error: {r.stderr.decode()[-200:]}")
        import shutil; shutil.copy(seg, dst)  # fallback: no label
    return os.path.exists(dst)

def build_with_sound(segs, sound_key, out):
    """Build xfade video (slideleft) and mix in the chosen sound at each transition."""
    n = len(segs)
    sound_file = f"{SNDDIR}/{sound_key}.aac"
    make_sound(sound_key, sound_file)

    # xfade chain
    inputs = []
    for s in segs: inputs += ["-i",s]
    offset = CLIP_DURATION - XFADE_DURATION
    filter_parts = []
    for i in range(n-1):
        t = offset*(i+1) - XFADE_DURATION
        in_a = f"[{i}:v]" if i==0 else f"[xf{i-1}]"
        in_b = f"[{i+1}:v]"
        out_label = f"[xf{i}]" if i < n-2 else "[vout]"
        filter_parts.append(
            f"{in_a}{in_b}xfade=transition=slideleft:duration={XFADE_DURATION}:offset={t:.3f}{out_label}"
        )
    video_only = f"{WORK}/vid_{sound_key}.mp4"
    r = subprocess.run([FFMPEG,"-y"]+inputs+[
        "-filter_complex",";".join(filter_parts),
        "-map","[vout]","-c:v","libx264","-preset","fast","-crf","21","-r","30",
        video_only],capture_output=True)
    if r.returncode != 0:
        print(f"  xfade error: {r.stderr.decode()[-300:]}")
        return False

    vid_dur = get_dur(video_only)
    transition_times = [offset*(i+1)-XFADE_DURATION/2 for i in range(n-1)]

    # Build SFX audio track
    audio_inputs = ["-f","lavfi","-i",f"aevalsrc=0:s=44100:c=stereo:d={vid_dur:.3f}"]
    afilt = ["[0:a]anull[base]"]
    for j,ts in enumerate(transition_times):
        audio_inputs += ["-i",sound_file]
        afilt.append(f"[{j+1}:a]adelay={int(ts*1000)}|{int(ts*1000)}[sw{j}]")
    mix_in = "[base]"+"".join(f"[sw{j}]" for j in range(len(transition_times)))
    afilt.append(f"{mix_in}amix=inputs={1+len(transition_times)}:normalize=0,volume=0.7[aout]")

    sfx_track = f"{WORK}/sfx_{sound_key}.aac"
    r = subprocess.run([FFMPEG,"-y"]+audio_inputs+[
        "-filter_complex",";".join(afilt),
        "-map","[aout]","-c:a","aac","-b:a","128k","-t",f"{vid_dur:.3f}",sfx_track],
        capture_output=True)
    if r.returncode != 0:
        print(f"  sfx error: {r.stderr.decode()[-200:]}")
        return False

    # Mux
    r = subprocess.run([FFMPEG,"-y","-i",video_only,"-i",sfx_track,
                        "-c:v","copy","-c:a","aac","-b:a","128k","-shortest",out],
                       capture_output=True)
    return r.returncode == 0

# ── Step 1: Download and encode clips (shared across all tests) ───────────────
print("Downloading 3 clips from Pexels…")
segs = []
for i,term in enumerate(SEARCH_TERMS):
    print(f"  [{i+1}/3] '{term}'", end=" ")
    url = fetch_pexels(term)
    if not url: print("✗"); continue
    raw = f"{RAWDIR}/clip{i}.mp4"
    seg = f"{SEGDIR}/seg{i}.mp4"
    if not download(url,raw): print("✗ download"); continue
    if not make_seg(raw,seg): print("✗ encode"); continue
    segs.append(seg)
    print("✓")
    time.sleep(0.3)

if len(segs) < 2:
    print("Need at least 2 clips. Abort.")
    sys.exit(1)

# ── Step 2: Build one video per sound ─────────────────────────────────────────
print(f"\nBuilding 5 test videos (one per sound)…")
labeled_segs = []
# Add label to first clip of each test variant
for i,(key,meta) in enumerate(SOUNDS.items()):
    print(f"  {meta['label']}…", end=" ")
    labeled_first = f"{SEGDIR}/labeled_{key}.mp4"
    add_label(segs[0], meta['label'], labeled_first)
    test_segs = [labeled_first] + segs[1:]
    out = f"{OUTDIR}/{key}.mp4"
    if build_with_sound(test_segs, key, out):
        labeled_segs.append(out)
        print("✓")
    else:
        print("✗")

# ── Step 3: Concatenate all 5 into one comparison video ──────────────────────
print(f"\nConcatenating {len(labeled_segs)} test segments into comparison video…")
concat_f = f"{WORK}/concat.txt"
with open(concat_f,"w") as f:
    for p in labeled_segs:
        f.write(f"file '{p}'\n")

r = subprocess.run([FFMPEG,"-y","-f","concat","-safe","0","-i",concat_f,
                    "-c:v","libx264","-preset","fast","-crf","21",
                    "-c:a","aac","-b:a","128k", OUTPUT],
                   capture_output=True)
if r.returncode != 0:
    print("CONCAT ERROR:", r.stderr.decode()[-400:])
    sys.exit(1)

final_dur = get_dur(OUTPUT)
size = os.path.getsize(OUTPUT)/1024/1024
print(f"\n✓  {OUTPUT}")
print(f"   Duration : {final_dur:.1f}s  |  Size: {size:.1f}MB")
print(f"   5 sounds tested — pick your favourite!")
