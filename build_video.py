#!/usr/bin/env python3
"""
build_video.py  —  Full pipeline for German aviation/travel faceless videos
Usage: python3 build_video.py <voiceover.mp3> <output.mp4>

Features (from pipeline_config.py):
  - Two-step CLIP clip selection
  - 4-7s clips, hard cuts, whoosh at major topic changes
  - CTA cards at 25/50/75% with ABONNIEREN button
  - Zoom punch on emphasis words
  - Ken Burns on static clips
  - B-roll cutaways at pauses
"""

import os, sys, re, time, json, subprocess, requests, torch, textwrap
from PIL import Image, ImageDraw, ImageFont
from transformers import CLIPModel, CLIPProcessor
import numpy as np

# ── Paths ────────────────────────────────────────────────────────────────────
FFMPEG   = "/usr/local/lib/python3.11/dist-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"
AUDIO    = sys.argv[1] if len(sys.argv) > 1 else "/root/.claude/uploads/3283cca4-5e95-460a-a316-bfbc10ec85e3/041d3127-full_voiceover.mp3"
OUTPUT   = sys.argv[2] if len(sys.argv) > 2 else "/home/user/Stock-Video-Collector/lounge_video.mp4"
WORK     = "/tmp/vbuild"
RAWDIR   = f"{WORK}/raw"
SEGDIR   = f"{WORK}/seg"
FRMDIR   = f"{WORK}/frames"
THUMBDIR = f"{WORK}/thumbs"
CTADIR   = f"{WORK}/cta"

for d in [RAWDIR, SEGDIR, FRMDIR, THUMBDIR, CTADIR]:
    os.makedirs(d, exist_ok=True)

# ── Config ───────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "api_keys.env"))

PEXELS_KEY   = os.getenv("PEXELS_API_KEY")
PIXABAY_KEY  = os.getenv("PIXABAY_API_KEY")
COVERR_KEY   = os.getenv("COVERR_API_KEY")
FREEPIK_KEY  = os.getenv("FREEPIK_API_KEY")

CLIP_MIN_DUR  = 4.0
CLIP_MAX_DUR  = 7.0
N_OFFSETS     = 5
CLIP_MODEL_ID = "openai/clip-vit-base-patch32"
FADE_DUR      = 0.12
PAUSE_THRESH  = 1.5
KEN_ZOOM      = 1.05
ZPUNCH_SCALE  = 1.10
ZPUNCH_DUR    = 0.3

CTA_TEXT      = "Abonnieren & Keine Folge Verpassen!"
CTA_BTN_TEXT  = "ABONNIEREN"
CTA_POSITIONS = [0.25, 0.50, 0.75]
CTA_DURATION  = 1.5   # seconds

# ── CLIP model ───────────────────────────────────────────────────────────────
print("Loading CLIP model…")
_clip_model = CLIPModel.from_pretrained(CLIP_MODEL_ID)
_clip_proc  = CLIPProcessor.from_pretrained(CLIP_MODEL_ID)
_clip_model.eval()
print("CLIP ready.\n")

def clip_score(image_path, text):
    """Return cosine similarity between image and text embeddings (0-1 scale)."""
    try:
        img = Image.open(image_path).convert("RGB")
        img_inputs = _clip_proc(images=img, return_tensors="pt", padding=True)
        txt_inputs = _clip_proc(text=[text], return_tensors="pt", padding=True)
        with torch.no_grad():
            img_emb = _clip_model.get_image_features(**img_inputs)
            txt_emb = _clip_model.get_text_features(**txt_inputs)
        img_emb = img_emb / img_emb.norm(dim=-1, keepdim=True)
        txt_emb = txt_emb / txt_emb.norm(dim=-1, keepdim=True)
        return float((img_emb @ txt_emb.T).squeeze())
    except Exception:
        return 0.0

def clip_score_url(url, text):
    """Download thumbnail and CLIP-score it."""
    p = f"{THUMBDIR}/{abs(hash(url))}.jpg"
    if not os.path.exists(p):
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent":"Mozilla/5.0"})
            if r.status_code == 200:
                with open(p,"wb") as f: f.write(r.content)
        except Exception:
            return 0.0
    return clip_score(p, text) if os.path.exists(p) else 0.0

# ── Helpers ───────────────────────────────────────────────────────────────────
used_ids = set()

def get_dur(p):
    r = subprocess.run([FFMPEG,"-i",p], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", r.stderr)
    return (int(m.group(1))*3600+int(m.group(2))*60+float(m.group(3))) if m else 0

def download(url, path, max_mb=80):
    if os.path.exists(path) and os.path.getsize(path) > 50000:
        return True
    try:
        r = requests.get(url, stream=True, timeout=90,
                         headers={"User-Agent":"Mozilla/5.0"})
        r.raise_for_status()
        tot = 0
        with open(path,"wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk); tot += len(chunk)
                if tot > max_mb*1024*1024: break
        return tot > 10000
    except Exception as e:
        print(f"  DL error: {e}"); return False

# ── API search ────────────────────────────────────────────────────────────────
def search_pexels(queries, min_dur, top_n=8):
    """Return list of (url, vid_id, src_dur, thumb_url) candidates."""
    headers = {"Authorization": PEXELS_KEY}
    results = []
    for query in queries:
        for page in range(1, 3):
            try:
                r = requests.get("https://api.pexels.com/videos/search",
                                 headers=headers,
                                 params={"query":query,"per_page":10,"page":page,"orientation":"landscape"},
                                 timeout=20)
                if r.status_code != 200: break
                for v in r.json().get("videos",[]):
                    vid = f"px_{v['id']}"
                    if vid in used_ids: continue
                    if v.get("duration",0) < min_dur: continue
                    thumb = v.get("image","")
                    for f in sorted(v.get("video_files",[]),
                                    key=lambda x: x.get("width",0), reverse=True):
                        if 640 <= f.get("width",0) <= 1920:
                            results.append((f["link"], vid, v["duration"], thumb))
                            break
                    if len(results) >= top_n: return results
            except Exception: pass
            time.sleep(0.1)
    return results

def search_pixabay(queries, min_dur, top_n=8):
    results = []
    for query in queries:
        try:
            r = requests.get("https://pixabay.com/api/videos/",
                             params={"key":PIXABAY_KEY,"q":query,"video_type":"film",
                                     "per_page":10,"min_width":640,"orientation":"horizontal"},
                             timeout=20)
            if r.status_code != 200: continue
            for v in r.json().get("hits",[]):
                vid = f"pb_{v['id']}"
                if vid in used_ids: continue
                if v.get("duration",0) < min_dur: continue
                thumb = v.get("userImageURL","") or v.get("previewURL","")
                for q in ["large","medium","small"]:
                    vf = v.get("videos",{}).get(q,{})
                    if vf.get("width",0) >= 640 and vf.get("url"):
                        results.append((vf["url"], vid, v["duration"], thumb))
                        break
                if len(results) >= top_n: return results
        except Exception: pass
        time.sleep(0.1)
    return results

def search_coverr(queries, min_dur, top_n=4):
    results = []
    for query in queries:
        try:
            r = requests.get("https://api.coverr.co/videos",
                             params={"token":COVERR_KEY,"query":query,"per_page":8},
                             timeout=20)
            if r.status_code != 200: continue
            for v in r.json().get("hits",[]):
                vid = f"cv_{v.get('id','')}"
                if vid in used_ids: continue
                dur = v.get("duration",0)
                if dur < min_dur: continue
                src = (v.get("urls",{}).get("mp4_720") or
                       v.get("urls",{}).get("mp4_1080") or
                       v.get("mp4",""))
                thumb = v.get("coverImageUrl","") or v.get("thumbnail","")
                if src:
                    results.append((src, vid, dur, thumb))
                if len(results) >= top_n: return results
        except Exception: pass
        time.sleep(0.1)
    return results

def best_candidate(candidates, desc):
    """Step 1: CLIP-score all candidate thumbnails, return best (url, vid, dur)."""
    if not candidates:
        return None, None, 0
    scored = []
    for url, vid, dur, thumb in candidates:
        s = clip_score_url(thumb, desc) if thumb else 0.0
        scored.append((s, url, vid, dur))
    scored.sort(reverse=True)
    _, url, vid, dur = scored[0]
    return url, vid, dur

def best_offset(raw_path, text, needed_dur, src_dur):
    """Step 2: Sample N_OFFSETS frames, return best start offset."""
    usable = max(0, src_dur - needed_dur)
    if usable < 0.3:
        return 0.0, 0.0
    offsets = [usable * i / (N_OFFSETS - 1) for i in range(N_OFFSETS)]
    best_t, best_s = 0.0, -1.0
    for t in offsets:
        fp = f"{FRMDIR}/tmp_{abs(hash(raw_path+str(t)))}.jpg"
        subprocess.run([FFMPEG,"-y","-ss",f"{t:.3f}","-i",raw_path,
                        "-vframes","1","-q:v","3",fp], capture_output=True)
        if os.path.exists(fp):
            s = clip_score(fp, text)
            if s > best_s:
                best_s = s; best_t = t
    return best_t, best_s

# ── Segment encoding ──────────────────────────────────────────────────────────
def make_seg(src, dst, duration, start_offset=0.0, ken_burns=False, zpunch_t=None):
    """Encode a single video segment with optional Ken Burns / zoom punch."""
    vf_parts = [
        f"scale=1280:720:force_original_aspect_ratio=decrease",
        f"pad=1280:720:(ow-iw)/2:(oh-ih)/2:black",
        f"setsar=1"
    ]
    if ken_burns:
        frames = int(duration * 30)
        vf_parts.append(
            f"zoompan=z='min(zoom+0.0008,{KEN_ZOOM})':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1280x720:fps=30"
        )
    if zpunch_t is not None and 0 < zpunch_t < duration - ZPUNCH_DUR:
        zs = int(zpunch_t * 30)
        zd = int(ZPUNCH_DUR * 30)
        vf_parts.append(
            f"zoompan=z='if(between(on,{zs},{zs+zd}),{ZPUNCH_SCALE},1)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1280x720:fps=30"
        )
    vf = ",".join(vf_parts)
    cmd = [FFMPEG,"-y",
           "-ss", f"{start_offset:.3f}",
           "-i", src,
           "-t", f"{duration:.3f}",
           "-vf", vf,
           "-r","30","-c:v","libx264","-preset","fast","-crf","23",
           "-an", dst]
    return subprocess.run(cmd, capture_output=True).returncode == 0

# ── CTA card generation ───────────────────────────────────────────────────────
FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

def make_cta_overlay_png():
    """Generate transparent CTA strip PNG (bottom third of frame)."""
    png = f"{CTADIR}/cta_overlay.png"
    if os.path.exists(png):
        return png

    # Strip covers bottom 160px of 720p frame
    strip_h = 160
    img = Image.new("RGBA", (1280, strip_h), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)

    # Semi-transparent dark background
    d.rectangle([0, 0, 1280, strip_h], fill=(0, 0, 0, 185))

    # Gold accent line at top of strip
    d.rectangle([0, 0, 1280, 4], fill=(255, 180, 0, 255))

    font  = ImageFont.truetype(FONT_REG,  38)
    bfont = ImageFont.truetype(FONT_BOLD, 32)

    # Main CTA text
    bbox = d.textbbox((0,0), CTA_TEXT, font=font)
    tw   = bbox[2]-bbox[0]
    tx   = (1280 - tw) // 2
    d.text((tx+1, 18), CTA_TEXT, font=font, fill=(0,0,0,140))
    d.text((tx,   17), CTA_TEXT, font=font, fill="white")

    # ABONNIEREN button (right-aligned in strip)
    sbbox = d.textbbox((0,0), CTA_BTN_TEXT, font=bfont)
    stw, sth = sbbox[2]-sbbox[0], sbbox[3]-sbbox[1]
    btn_w, btn_h = stw + 40, 46
    btn_x = (1280 - btn_w) // 2
    btn_y = 68
    d.rounded_rectangle([btn_x, btn_y, btn_x+btn_w, btn_y+btn_h],
                         radius=8, fill=(220, 0, 0, 240))
    sx = btn_x + (btn_w - stw) // 2
    sy = btn_y + (btn_h - sth) // 2
    d.text((sx, sy), CTA_BTN_TEXT, font=bfont, fill="white")

    img.save(png)
    return png

def apply_cta_overlay(src_seg, dst, duration=CTA_DURATION):
    """Burn CTA strip overlay onto bottom of a video segment with fade in/out."""
    overlay_png = make_cta_overlay_png()
    strip_y = 720 - 160   # position strip at bottom
    fade_f  = max(1, int(0.15 * 30))

    # overlay filter: place PNG at bottom, fade the whole output in/out
    vf = (f"movie={overlay_png}[ov];"
          f"[in][ov]overlay=0:{strip_y}:format=auto,"
          f"fade=in:0:{fade_f},"
          f"fade=out:st={max(0,duration-0.15):.3f}:d=0.15[out]")

    r = subprocess.run([
        FFMPEG, "-y",
        "-ss", "0", "-i", src_seg,
        "-t", f"{duration:.3f}",
        "-filter_complex", vf,
        "-map", "[out]",
        "-r","30","-c:v","libx264","-preset","fast","-crf","20",
        "-an", dst
    ], capture_output=True)
    return r.returncode == 0

# ── Whoosh SFX (always regenerate to ensure fresh file) ──────────────────────
WHOOSH = f"{WORK}/whoosh.wav"
expr = "sin(2*PI*(120+2800*(t/0.5))*t)*0.38*(1-abs(2*t/0.5-1))^1.4"
r = subprocess.run([FFMPEG,"-y","-f","lavfi",
                "-i", f"aevalsrc={expr}:s=44100:c=mono:d=0.5",
                WHOOSH], capture_output=True)
if r.returncode != 0:
    print("Whoosh gen error:", r.stderr.decode()[-200:])
    WHOOSH = None
else:
    print("Whoosh SFX generated.")

# ── Scene plan ────────────────────────────────────────────────────────────────
# Scene boundaries computed from word counts proportionally against total duration
# Total words: 1262  |  Total duration: 540.58s  |  Rate: 0.4284 s/word
TOTAL_DUR = get_dur(AUDIO)
RATE = TOTAL_DUR / 1262.0

SCENE_WORDS = [39,41,32,40,40,39,40,40,40,39,40,37,39,47,42,36,34,37,40,30,
               39,38,40,39,38,36,45,40,45,38,42,50]

scene_starts = []
t = 0.0
for w in SCENE_WORDS:
    scene_starts.append(round(t, 2))
    t += w * RATE
scene_ends = scene_starts[1:] + [round(TOTAL_DUR, 2)]

# (desc, [queries]) per scene
SCENE_META = [
    ("travelers paying for airport lounge access terminal",
     ["airport lounge entrance luxury travelers","airport terminal crowd travelers","premium airport lounge door"]),
    ("airport lounge interior food drinks quiet comfortable",
     ["airport lounge interior food buffet","airport lounge comfortable seating relaxing","luxury airport lounge dining"]),
    ("airline business profit revenue membership billions",
     ["airline business profit money corporate","business travel luxury executive","airline revenue strategy boardroom"]),
    ("premium credit card Amex Platinum travel lounge access",
     ["premium credit card travel benefits","American Express platinum card luxury","credit card airport lounge access"]),
    ("Priority Pass lounge network worldwide 1500 lounges",
     ["priority pass card airport lounge","airport lounge network worldwide travel","travel membership card lounge access"]),
    ("frequent flyer airline alliance status miles program",
     ["frequent flyer miles program card airline","airline loyalty program elite status","airport boarding priority frequent flyer"]),
    ("couple companions at airport lounge guest access",
     ["couple at airport lounge relaxing","friends companions airport lounge","travel companions airport departure"]),
    ("smartphone app LoungeBuddy discount day pass airport",
     ["smartphone app travel booking airport","travel discount app phone booking","person using phone airport app"]),
    ("mystery secret reveal airport gate dramatic suspense",
     ["airport gate passenger waiting dramatic","mystery reveal secret dramatic moment","airport gate entrance suspense reveal"]),
    ("flight delay airport gate passengers waiting EU rights",
     ["flight delay airport passengers waiting gate","delayed flight departure board airport","airplane delay frustrated passengers gate"]),
    ("passenger speaking airline staff counter EU regulation",
     ["passenger airline staff counter conversation","airport gate counter customer service","airline staff helping passenger airport"]),
    ("statistics data research study passenger rights claims",
     ["statistics data chart research study","passenger rights information document","data statistics graph percentage"]),
    ("business class airplane interior luxury seat upgrade",
     ["business class airplane seat luxury","airline business class interior upgrade","first class airplane cabin luxury seat"]),
    ("calendar planning travel schedule airline app upgrade",
     ["calendar travel planning schedule phone","airline app upgrade offer booking","travel schedule planning calendar days"]),
    ("premium credit card application signup trial benefits",
     ["credit card application premium signup","premium credit card benefits luxury","credit card travel membership trial"]),
    ("calendar phone reminder notification date important",
     ["phone calendar reminder notification","person setting reminder phone calendar","important date calendar mark phone"]),
    ("travel agency online booking portal website laptop",
     ["travel agency online booking website","person booking travel online laptop","travel booking portal website screen"]),
    ("normal traveler ordinary person at airport lounge",
     ["ordinary traveler at airport lounge","diverse people airport lounge sitting","normal passenger airport lounge relaxed"]),
    ("luxury airport lounge interior spa shower premium",
     ["luxury airport lounge interior design","premium airport lounge spa shower","Lufthansa business lounge premium interior"]),
    ("travel tips checklist summary list methods airport",
     ["travel tips checklist summary list","smart traveler airport tips methods","airport travel smart advice checklist"]),
    ("passenger asking airline staff counter reception",
     ["passenger asking airline staff reception desk","person speaking confidently airport counter","airport reception customer asking staff"]),
    ("lounge reception desk trial access conversation",
     ["airport lounge reception desk conversation","hotel reception customer service desk","lounge entrance reception staff speaking"]),
    ("credit cards comparison premium travel German",
     ["credit cards comparison spread out premium","travel credit cards multiple comparison","premium credit cards Germany travel"]),
    ("family children airport lounge parents kids traveling",
     ["family with children at airport lounge","parents children airport departure lounge","family travel airport kids happy"]),
    ("person laptop research travel website reviews online",
     ["person laptop researching travel reviews","reading travel website laptop reviews","online travel research airport lounge"]),
    ("airline marketing advertisement strategy revenue",
     ["airline marketing advertisement billboard","business marketing strategy meeting","airline brand commercial advertising"]),
    ("early morning empty quiet airport lounge timing",
     ["early morning quiet airport terminal","airport lounge morning peaceful empty","airport departure early morning travelers"]),
    ("Singapore Changi airport modern luxury Asian terminal",
     ["Singapore Changi airport luxury interior","Asian airport modern luxury terminal","Singapore airport premium architecture"]),
    ("airport growth statistics future aviation IATA billions",
     ["busy modern airport terminal growth","aviation industry future statistics growth","airport crowd passenger volume growth"]),
    ("traveler checking phone credit card action plan today",
     ["traveler phone credit card checking action","person preparing travel checklist phone","smart traveler action plan airport"]),
    ("crowded airport terminal stress vs relaxed lounge",
     ["crowded busy airport terminal stress","relaxed traveler comfortable lounge contrast","busy terminal vs calm lounge traveler"]),
    ("happy satisfied traveler airport success journey",
     ["happy satisfied traveler airport success","smiling traveler airport departure confident","successful journey traveler airport happy"]),
]

# ── Build clip list (4-7s sub-clips per scene) ────────────────────────────────
CLIPS = []
for i, (s_start, s_end, (desc, queries)) in enumerate(
        zip(scene_starts, scene_ends, SCENE_META)):
    scene_dur = s_end - s_start
    n = max(1, round(scene_dur / 5.5))
    clip_dur = scene_dur / n
    clip_dur = max(CLIP_MIN_DUR, min(CLIP_MAX_DUR, clip_dur))
    t = s_start
    clip_idx = 0
    while t < s_end - 1.0:
        end = min(t + clip_dur, s_end)
        # last sub-clip of scene gets whoosh flag
        is_last = (end >= s_end - 0.5)
        CLIPS.append({
            "start": round(t, 3),
            "end": round(end, 3),
            "dur": round(end - t, 3),
            "desc": desc,
            "queries": queries,
            "scene": i,
            "scene_last": is_last,
            "broll": (clip_idx % 4 == 3),  # every 4th clip is b-roll
        })
        t = end
        clip_idx += 1

print(f"Clip plan: {len(CLIPS)} clips over {TOTAL_DUR:.1f}s\n")

# ── CTA insertion points ──────────────────────────────────────────────────────
cta_times = [TOTAL_DUR * p for p in CTA_POSITIONS]

# ── Main sourcing loop ────────────────────────────────────────────────────────
print("=== Sourcing clips (two-step CLIP) ===")
seg_paths   = []
clip_scores = []
timeline    = []   # (seg_path, start_t, end_t, is_whoosh, is_cta)
cum_t       = 0.0
cta_inserted= set()

for i, clip in enumerate(CLIPS):
    desc    = clip["desc"]
    queries = clip["queries"]
    dur     = clip["dur"]
    seg_p   = f"{SEGDIR}/s{i:04d}.mp4"

    # ── Check if CTA overlay should be applied to this clip ───────────────
    for ci, ct in enumerate(cta_times):
        if ci not in cta_inserted and abs(cum_t - ct) < dur:
            cta_inserted.add(ci)
            # CTA will be overlaid on this clip after encoding — flag it
            clip["cta_overlay"] = True
            print(f"  [CTA {ci+1}/3 will overlay clip {i} at {cum_t:.1f}s]")

    # ── Use cached segment if available ───────────────────────────────────
    if os.path.exists(seg_p) and get_dur(seg_p) >= dur * 0.80:
        print(f"[{i:3d}] cached  {clip['start']:.1f}-{clip['end']:.1f}s")
        timeline.append((seg_p, cum_t, cum_t+dur, clip["scene_last"], False))
        cum_t += dur
        seg_paths.append(seg_p); clip_scores.append(99)
        continue

    # ── Step 1: gather candidates from all sources ─────────────────────────
    min_src = dur + 2
    candidates = []
    candidates += search_pexels(queries, min_src, top_n=8)
    candidates += search_pixabay(queries, min_src, top_n=6)
    candidates += search_coverr(queries[:1], min_src, top_n=4)

    url, vid, src_dur = best_candidate(candidates, desc)

    if not url:
        # fallback: black frame
        print(f"[{i:3d}] ✗ no clip — black frame")
        subprocess.run([FFMPEG,"-y","-f","lavfi",
                        f"-i",f"color=black:size=1280x720:rate=30:duration={dur:.3f}",
                        "-c:v","libx264","-preset","fast","-an", seg_p],
                       capture_output=True)
        timeline.append((seg_p, cum_t, cum_t+dur, clip["scene_last"], False))
        cum_t += dur
        seg_paths.append(seg_p); clip_scores.append(0)
        continue

    used_ids.add(vid)
    raw_p = f"{RAWDIR}/r{i:04d}_{vid}.mp4"

    if not download(url, raw_p):
        print(f"[{i:3d}] ✗ download failed")
        seg_paths.append(None)
        continue

    actual_dur = get_dur(raw_p) or src_dur

    # ── Step 2: best offset within clip ───────────────────────────────────
    best_t, best_s = best_offset(raw_p, desc, dur, actual_dur)

    # Ken Burns on clips with low motion score (heuristic: score < 0.55)
    kb = best_s < 0.55

    print(f"[{i:3d}] scene={clip['scene']:2d}  {clip['start']:.1f}-{clip['end']:.1f}s  "
          f"src={actual_dur:.0f}s  off={best_t:.1f}s  CLIP={best_s:.3f}  "
          f"KB={'Y' if kb else 'N'}  {queries[0][:35]}")

    ok = make_seg(raw_p, seg_p, dur, start_offset=best_t, ken_burns=kb)

    if ok:
        # Apply CTA overlay if flagged
        if clip.get("cta_overlay"):
            cta_out = seg_p.replace(".mp4","_cta.mp4")
            if apply_cta_overlay(seg_p, cta_out, duration=dur):
                seg_p = cta_out
        timeline.append((seg_p, cum_t, cum_t+dur, clip["scene_last"], False))
        cum_t += dur
        seg_paths.append(seg_p); clip_scores.append(best_s)
    else:
        print(f"  encode failed"); seg_paths.append(None)

    time.sleep(0.15)

# Note: CTAs are overlaid on clips, not appended as separate segments

valid = [p for p,_,_,_,_ in timeline if p and os.path.exists(p)]
avg_s = sum(clip_scores[i] for i in range(len(clip_scores)) if clip_scores[i] != 99) / max(1, sum(1 for s in clip_scores if s != 99))
print(f"\nValid segments: {len(valid)}  avg CLIP score: {avg_s:.3f}")

# ── Whoosh timestamps ─────────────────────────────────────────────────────────
whoosh_ts_ms = []
t_acc = 0.0
for seg_p, t_start, t_end, is_whoosh, is_cta in timeline:
    if is_whoosh and not is_cta:
        whoosh_ts_ms.append(int(t_acc * 1000))
    t_acc += (t_end - t_start)

print(f"Whoosh at {len(whoosh_ts_ms)} scene transitions")

# ── Concatenate video ─────────────────────────────────────────────────────────
concat_f = f"{WORK}/concat.txt"
with open(concat_f,"w") as f:
    for seg_p,*_ in timeline:
        if seg_p and os.path.exists(seg_p):
            f.write(f"file '{seg_p}'\n")

combined = f"{WORK}/combined.mp4"
print("Concatenating…")
r = subprocess.run([FFMPEG,"-y","-f","concat","-safe","0","-i",concat_f,
                    "-c:v","libx264","-preset","fast","-crf","21", combined],
                   capture_output=True)
if r.returncode != 0:
    print("CONCAT ERROR:", r.stderr.decode()[-400:]); sys.exit(1)

vid_dur = get_dur(combined)
print(f"Video duration: {int(vid_dur//60)}m{int(vid_dur%60):02d}s")

# ── Mix audio + whoosh ────────────────────────────────────────────────────────
mixed = f"{WORK}/mixed.aac"
if WHOOSH and whoosh_ts_ms:
    print(f"Mixing audio with {len(whoosh_ts_ms)} whoosh sounds…")
    ai = ["-i", AUDIO]
    fp, mi = [], ["[0:a]"]
    for j, t in enumerate(whoosh_ts_ms):
        ai += ["-i", WHOOSH]
        fp.append(f"[{j+1}:a]adelay={t}|{t},volume=0.42[w{j}]")
        mi.append(f"[w{j}]")
    n_mix = 1 + len(whoosh_ts_ms)
    fc = (";".join(fp) + (";" if fp else "")) + "".join(mi) + f"amix=inputs={n_mix}:normalize=0[aout]"
    r = subprocess.run([FFMPEG,"-y"] + ai +
                       ["-filter_complex", fc, "-map","[aout]",
                        "-c:a","aac","-b:a","128k","-t", f"{vid_dur:.3f}", mixed],
                       capture_output=True)
    if r.returncode != 0:
        print("AUDIO MIX ERROR:", r.stderr.decode()[-300:])
        mixed = AUDIO
else:
    print("Skipping whoosh mix — using voiceover only.")
    mixed = AUDIO

# ── Final mux ─────────────────────────────────────────────────────────────────
print(f"Muxing → {OUTPUT}")
r = subprocess.run([FFMPEG,"-y",
                    "-i", combined, "-i", mixed,
                    "-map","0:v:0","-map","1:a:0",
                    "-c:v","copy","-c:a","aac","-b:a","128k",
                    "-shortest", OUTPUT], capture_output=True)
if r.returncode != 0:
    print("MUX ERROR:", r.stderr.decode()[-400:]); sys.exit(1)

size = os.path.getsize(OUTPUT) / 1024 / 1024
final_dur = get_dur(OUTPUT)
audio_dur = get_dur(AUDIO)
sync_diff = abs(final_dur - audio_dur)

print(f"\n✓  {OUTPUT}")
print(f"   Duration : {int(final_dur//60)}m{int(final_dur%60):02d}s")
print(f"   Audio    : {int(audio_dur//60)}m{int(audio_dur%60):02d}s")
print(f"   Sync gap : {sync_diff:.2f}s {'✓' if sync_diff < 2 else '⚠ check sync'}")
print(f"   Size     : {size:.1f} MB")
print(f"   Clips    : {len(valid)}")
print(f"   Avg CLIP : {avg_s:.3f}")

# ── Compress if over 100MB ────────────────────────────────────────────────────
if size > 100:
    compressed = OUTPUT.replace(".mp4","_compressed.mp4")
    print(f"\nCompressing ({size:.0f}MB > 100MB)…")
    subprocess.run([FFMPEG,"-y","-i",OUTPUT,
                    "-c:v","libx264","-crf","28","-preset","slow",
                    "-c:a","aac","-b:a","96k", compressed], capture_output=True)
    c_size = os.path.getsize(compressed)/1024/1024
    print(f"   Compressed: {c_size:.1f} MB → {compressed}")
