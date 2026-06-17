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

import os, sys, re, time, json, random, subprocess, requests, torch, textwrap
import anthropic
from PIL import Image, ImageDraw, ImageFont
from transformers import CLIPModel, CLIPProcessor
import numpy as np

# ── Paths ────────────────────────────────────────────────────────────────────
FFMPEG   = "/usr/local/lib/python3.11/dist-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"
AUDIO    = sys.argv[1] if len(sys.argv) > 1 else "/root/.claude/uploads/c6774b2d-5668-54de-9a4f-188c80ca845c/bb4a8f28-full_voiceover.mp3"
OUTPUT   = sys.argv[2] if len(sys.argv) > 2 else "/home/user/Stock-Video-Collector/wm2026_flughafenscanner_video.mp4"
WORK     = "/tmp/vbuild"
RAWDIR   = f"{WORK}/raw"
SEGDIR   = f"{WORK}/seg"
FRMDIR   = f"{WORK}/frames"
THUMBDIR = f"{WORK}/thumbs"
CTADIR   = f"{WORK}/cta"
IMGDIR   = f"{WORK}/img"

for d in [RAWDIR, SEGDIR, FRMDIR, THUMBDIR, CTADIR, IMGDIR]:
    os.makedirs(d, exist_ok=True)

# ── Config ───────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "api_keys.env"))

PEXELS_KEY       = os.getenv("PEXELS_API_KEY")
PIXABAY_KEY      = os.getenv("PIXABAY_API_KEY")
COVERR_KEY           = os.getenv("COVERR_API_KEY")
VECTEEZY_SECRET_KEY  = os.getenv("VECTEEZY_SECRET_KEY")
VECTEEZY_ACCOUNT_ID  = os.getenv("VECTEEZY_ACCOUNT_ID")
FREEPIK_KEY          = os.getenv("FREEPIK_API_KEY")
SHUTTERSTOCK_CLIENT_ID     = os.getenv("SHUTTERSTOCK_CLIENT_ID")
SHUTTERSTOCK_CLIENT_SECRET = os.getenv("SHUTTERSTOCK_CLIENT_SECRET")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY")
UNSPLASH_KEY     = os.getenv("UNSPLASH_API_KEY")

# B-roll config
BROLL_EVERY_N_CLIPS = 8    # insert b-roll after every N main clips
BROLL_DURATION      = 2.0  # seconds per b-roll cutaway

CLIP_MIN_DUR  = 4.0
CLIP_MAX_DUR  = 7.0
N_OFFSETS     = 5
CLIP_MODEL_ID = "openai/clip-vit-base-patch32"
FADE_DUR      = 0.12
PAUSE_THRESH  = 1.5
KEN_ZOOM      = 1.05
ZPUNCH_SCALE  = 1.10
ZPUNCH_DUR    = 0.3

CTA_POSITIONS = [0.25, 0.50, 0.75]
CTA_DURATION  = 4.2   # seconds (slide_in + hold + slide_out)

# Image fallback: when no source returns a good-enough video clip for a slot,
# fetch a still image (Pexels/Pixabay/Unsplash) and apply a Ken Burns move.
# Hard rule: never more than MAX_CONSECUTIVE_IMAGES stills in a row (avoid a
# slideshow feel). Only triggers when the best video CLIP score is below
# IMAGE_FALLBACK_THRESHOLD *and* the best image actually scores higher.
IMAGE_FALLBACK_THRESHOLD = 0.18
MAX_CONSECUTIVE_IMAGES   = 2

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
        inputs = _clip_proc(text=[text], images=img, return_tensors="pt", padding=True)
        with torch.no_grad():
            out = _clip_model(**inputs)
            img_emb = out.image_embeds
            txt_emb = out.text_embeds
        img_emb = img_emb / img_emb.norm(dim=-1, keepdim=True)
        txt_emb = txt_emb / txt_emb.norm(dim=-1, keepdim=True)
        return float((img_emb @ txt_emb.T).squeeze())
    except Exception:
        return 0.0

_anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None
_broll_cache = {}

def get_broll_queries(scene_text):
    """Use Claude to generate 3 b-roll cutaway search queries for a scene."""
    if scene_text in _broll_cache:
        return _broll_cache[scene_text]
    if not _anthropic_client:
        return []
    try:
        msg = _anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role":"user","content":
                f"""Given this voiceover scene text (German aviation/travel video):
"{scene_text}"

Generate exactly 3 short English search queries for close-up b-roll cutaway shots that visually match this scene.
Rules: each query max 5 words, focus on close-up detail shots, no people's faces.
Reply with ONLY 3 lines, one query per line, nothing else."""}]
        )
        queries = [l.strip() for l in msg.content[0].text.strip().split("\n") if l.strip()][:3]
        _broll_cache[scene_text] = queries
        return queries
    except Exception as e:
        print(f"  B-roll query error: {e}")
        return []

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
# Cross-video dedup with a cooldown window: a clip used in a recent video is
# excluded, but becomes reusable again after DEDUP_COOLDOWN builds. This keeps
# footage fresh across videos without permanently exhausting the (limited,
# aviation-themed) clip pool. History maps clip_id -> build number it was used.
USED_HISTORY_FILE = os.path.join(os.path.dirname(__file__), ".used_clips.json")
DEDUP_COOLDOWN    = 7   # number of recent builds whose clips are blocked

def _load_history():
    """Return (history dict {clip_id: build_no}, current_build_no)."""
    try:
        with open(USED_HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "clips" in data:
            hist = data["clips"]
            cur  = int(data.get("build", max(hist.values(), default=0))) + 1
            return hist, cur
        # legacy flat-list format → treat all as build 1
        if isinstance(data, list):
            return {cid: 1 for cid in data}, 2
    except Exception:
        pass
    return {}, 1

def _save_history(history, build_no):
    try:
        with open(USED_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({"build": build_no, "clips": history}, f)
    except Exception as e:
        print(f"  Warning: could not save clip history: {e}")

_clip_history, BUILD_NO = _load_history()
# Block only clips used within the cooldown window; older ones are reusable.
used_ids = {cid for cid, b in _clip_history.items() if BUILD_NO - b < DEDUP_COOLDOWN}
print(f"Build #{BUILD_NO}: {len(used_ids)} clips blocked "
      f"(cooldown {DEDUP_COOLDOWN} builds), {len(_clip_history)} total in history")

def get_dur(p):
    r = subprocess.run([FFMPEG,"-i",p], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", r.stderr)
    return (int(m.group(1))*3600+int(m.group(2))*60+float(m.group(3))) if m else 0

def download(url, path, max_mb=80):
    if os.path.exists(path) and os.path.getsize(path) > 50000:
        return True
    try:
        import signal
        def _timeout(sig, frame): raise TimeoutError("download stalled")
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(60)
        try:
            r = requests.get(url, stream=True, timeout=30,
                             headers={"User-Agent":"Mozilla/5.0"})
            r.raise_for_status()
            tot = 0
            with open(path,"wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk); tot += len(chunk)
                    if tot > max_mb*1024*1024: break
        finally:
            signal.alarm(0)
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
                             headers={"Authorization": f"Bearer {COVERR_KEY}"},
                             params={"query":query,"per_page":8},
                             timeout=20)
            if r.status_code != 200: continue
            for v in r.json().get("hits",[]):
                vid = f"cv_{v.get('id','')}"
                if vid in used_ids: continue
                dur = float(v.get("duration") or 0)
                if dur < min_dur: continue
                thumb = v.get("thumbnail","") or v.get("poster","")
                # fetch detail to get download URL
                vid_id = v.get("id","")
                try:
                    det = requests.get(f"https://api.coverr.co/videos/{vid_id}",
                                       headers={"Authorization": f"Bearer {COVERR_KEY}"},
                                       timeout=10).json()
                    src = (det.get("urls",{}).get("mp4") or
                           det.get("urls",{}).get("mp4_preview",""))
                except Exception:
                    src = ""
                if src:
                    results.append((src, vid, dur, thumb))
                if len(results) >= top_n: return results
        except Exception: pass
        time.sleep(0.1)
    return results

def search_freepik(queries, min_dur, top_n=6):
    results = []
    for query in queries:
        try:
            r = requests.get("https://api.freepik.com/v1/videos",
                             headers={"X-Freepik-API-Key": FREEPIK_KEY,
                                      "Accept-Language": "en-US"},
                             params={"term": query, "per_page": 10},
                             timeout=20)
            if r.status_code != 200: continue
            for v in r.json().get("data", []):
                vid = f"fp_{v.get('id','')}"
                if vid in used_ids: continue
                dur = v.get("duration", 0)
                if dur < min_dur: continue
                thumb = v.get("image", {}).get("source", {}).get("url", "")
                src   = (v.get("downloads", {}).get("mp4_fullhd", {}).get("url") or
                         v.get("downloads", {}).get("mp4_hd", {}).get("url") or
                         v.get("downloads", {}).get("mp4", {}).get("url", ""))
                if src:
                    results.append((src, vid, dur, thumb))
                if len(results) >= top_n: return results
        except Exception: pass
        time.sleep(0.1)
    return results

def search_shutterstock(queries, min_dur, top_n=6):
    import base64
    creds = base64.b64encode(
        f"{SHUTTERSTOCK_CLIENT_ID}:{SHUTTERSTOCK_CLIENT_SECRET}".encode()
    ).decode()
    results = []
    for query in queries:
        try:
            r = requests.get("https://api.shutterstock.com/v2/videos/search",
                             headers={"Authorization": f"Basic {creds}"},
                             params={"query": query, "per_page": 10,
                                     "min_duration": int(min_dur),
                                     "aspect_ratio": "16_9"},
                             timeout=20)
            if r.status_code != 200: continue
            for v in r.json().get("data", []):
                vid = f"ss_{v.get('id','')}"
                if vid in used_ids: continue
                dur = v.get("duration", 0)
                if dur < min_dur: continue
                thumb = v.get("assets", {}).get("thumb_jpg", {}).get("url", "")
                src = v.get("assets", {}).get("preview_mp4", {}).get("url", "")
                if src:
                    results.append((src, vid, dur, thumb))
                if len(results) >= top_n: return results
        except Exception: pass
        time.sleep(0.1)
    return results

_MIXKIT_STOPWORDS = {"the","a","an","of","on","in","at","to","and","with","for",
                     "person","people","close","up","shot","footage","video","clip"}

def _mixkit_slugs(queries):
    """Build candidate Mixkit tag slugs. Mixkit URLs are tag-based, so full
    phrases ('flight-attendant') usually 404 to an empty page — single
    keywords ('airport', 'pilot', 'airplane') are far more likely to be real
    tags. Try full slug first, then significant individual words."""
    slugs = []
    for query in queries:
        full = query.replace(" ", "-").lower()
        if full not in slugs:
            slugs.append(full)
        for word in query.lower().split():
            word = re.sub(r'[^a-z]', '', word)
            if len(word) >= 4 and word not in _MIXKIT_STOPWORDS and word not in slugs:
                slugs.append(word)
    return slugs

def search_mixkit(queries, min_dur, top_n=6):
    """Scrape Mixkit free stock video search results (tag-based pages)."""
    results = []
    for slug in _mixkit_slugs(queries):
        try:
            r = requests.get(f"https://mixkit.co/free-stock-video/{slug}/",
                             headers={"User-Agent":"Mozilla/5.0"},
                             timeout=20)
            if r.status_code != 200: continue
            # Capture every distinct video ID referenced on the page, regardless
            # of the resolution listed (720 mp4 exists for all of them).
            ids = []
            for m in re.findall(r'assets\.mixkit\.co/videos/(\d+)/', r.text):
                if m not in ids:
                    ids.append(m)
            for vid_id in ids:
                vid = f"mx_{vid_id}"
                if vid in used_ids: continue
                src   = f"https://assets.mixkit.co/videos/{vid_id}/{vid_id}-720.mp4"
                thumb = f"https://assets.mixkit.co/videos/{vid_id}/{vid_id}-thumb-360-0.jpg"
                dur = 15  # Mixkit clips are typically 10-30s; duration not in listing
                if dur >= min_dur:
                    results.append((src, vid, dur, thumb))
                if len(results) >= top_n: return results
        except Exception: pass
        time.sleep(0.1)
    return results

def search_vecteezy(queries, min_dur, top_n=6):
    """Search Vecteezy API v2 for stock videos."""
    if not VECTEEZY_SECRET_KEY or not VECTEEZY_ACCOUNT_ID:
        return []
    results = []
    headers = {
        "Authorization": f"Bearer {VECTEEZY_SECRET_KEY}",
        "Accept": "application/json",
    }
    for query in queries:
        try:
            r = requests.get(
                f"https://api.vecteezy.com/v2/{VECTEEZY_ACCOUNT_ID}/resources",
                params={"term": query, "content_type": "video", "per_page": top_n},
                headers=headers, timeout=15,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            for item in data.get("resources", []):
                vid = f"vz_{item['id']}"
                if vid in used_ids:
                    continue
                # Get signed download URL
                dl_r = requests.get(
                    f"https://api.vecteezy.com/v2/{VECTEEZY_ACCOUNT_ID}/resources/{item['id']}/download",
                    params={"size": "small"},
                    headers=headers, timeout=10,
                )
                if dl_r.status_code != 200:
                    continue
                dl_data = dl_r.json()
                url = dl_data.get("url") or dl_data.get("inline_url")
                if not url:
                    continue
                thumb = item.get("thumbnail_url", "")
                # Estimate duration from file size (~3MB/s for 720p video)
                sizes = item.get("file_metadata", {}).get("available_file_types", [])
                size_bytes = sizes[0].get("size_in_bytes", 0) if sizes else 0
                est_dur = max(5, int(size_bytes / (3 * 1024 * 1024))) if size_bytes else 15
                if est_dur >= min_dur:
                    results.append((url, vid, est_dur, thumb))
                if len(results) >= top_n:
                    return results
        except Exception as e:
            print(f"  Vecteezy error: {e}")
        time.sleep(0.1)
    return results

# Diversity weighting: penalize sources that already dominate the video so
# relevant clips from under-used sources get a fair chance. Tuned against the
# typical CLIP score spread (~0.18-0.33) so it nudges, never overrides, relevance.
DIVERSITY_PENALTY = 0.12
SOURCE_HARD_CAP   = 0.50   # no single source can exceed 50% of total clips

def best_candidate(candidates, desc, source_counts=None, total_clips=0):
    """Step 1: CLIP-score all candidate thumbnails, return best (url, vid, dur).
    Applies diversity penalty to over-represented sources and enforces a hard cap."""
    if not candidates:
        return None, None, 0
    scored = []
    for url, vid, dur, thumb in candidates:
        s = clip_score_url(thumb, desc) if thumb else 0.0
        src = vid.split("_")[0] if "_" in vid else "?"
        if source_counts and total_clips > 0:
            share = source_counts.get(src, 0) / total_clips
            s -= DIVERSITY_PENALTY * share
            if total_clips >= 10 and share >= SOURCE_HARD_CAP:
                s -= 0.5  # effectively disqualifies this source
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
XFADE_DURATION = 0.4   # seconds overlap for slideleft xfade between every clip

def make_seg(src, dst, duration, start_offset=0.0, scene_last=False, ken_burns=False, zpunch_t=None):
    """Encode a single video segment. No fades baked in — xfade handles transitions."""
    vf = ("scale=1280:720:force_original_aspect_ratio=decrease,"
          "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,setsar=1")
    cmd = [FFMPEG,"-y",
           "-ss", f"{start_offset:.3f}",
           "-i", src,
           "-t", f"{duration:.3f}",
           "-vf", vf,
           "-r","30","-c:v","libx264","-preset","fast","-crf","23",
           "-an", dst]
    return subprocess.run(cmd, capture_output=True).returncode == 0


def search_images(queries, top_n=12):
    """Return list of (img_url, img_id, thumb_url) still-image candidates.
    Order: Pexels photos → Pixabay photos → Unsplash (last, rate-limited 50/hr).
    Only photos suitable for Ken Burns — no vectors/graphics."""
    results = []
    # ── Pexels photos ──
    try:
        for query in queries:
            r = requests.get("https://api.pexels.com/v1/search",
                             headers={"Authorization": PEXELS_KEY},
                             params={"query":query,"per_page":8,"orientation":"landscape"},
                             timeout=20)
            if r.status_code == 200:
                for p in r.json().get("photos",[]):
                    iid = f"pximg_{p['id']}"
                    if iid in used_ids: continue
                    src  = p.get("src",{})
                    full = src.get("large2x") or src.get("large") or src.get("original")
                    if not full: continue
                    results.append((full, iid, src.get("medium") or full))
            if len(results) >= top_n: break
            time.sleep(0.1)
    except Exception: pass
    # ── Pixabay photos ──
    try:
        for query in queries:
            r = requests.get("https://pixabay.com/api/",
                             params={"key":PIXABAY_KEY,"q":query,"image_type":"photo",
                                     "per_page":8,"min_width":1280,"orientation":"horizontal"},
                             timeout=20)
            if r.status_code == 200:
                for p in r.json().get("hits",[]):
                    iid = f"pbimg_{p['id']}"
                    if iid in used_ids: continue
                    full = p.get("largeImageURL") or p.get("webformatURL")
                    if not full: continue
                    results.append((full, iid, p.get("webformatURL") or full))
            if len(results) >= top_n: break
            time.sleep(0.1)
    except Exception: pass
    # ── Unsplash (last; demo tier = 50 req/hr, so query only the top term) ──
    if UNSPLASH_KEY:
        try:
            r = requests.get("https://api.unsplash.com/search/photos",
                             headers={"Authorization": f"Client-ID {UNSPLASH_KEY}"},
                             params={"query":queries[0],"per_page":5,"orientation":"landscape"},
                             timeout=20)
            if r.status_code == 200:
                for p in r.json().get("results",[]):
                    iid = f"usimg_{p['id']}"
                    if iid in used_ids: continue
                    raw = p.get("urls",{}).get("raw")
                    if not raw: continue
                    full = f"{raw}&w=1920&fm=jpg&q=85"
                    results.append((full, iid, p["urls"].get("small") or full))
        except Exception: pass
    return results


def make_image_segment(src, dst, duration, scene_last=False):
    """Encode a still image into a video segment with a randomized Ken Burns move.
    Uses zoompan with d=1 driven by the global output frame counter `on`, so the
    motion is smooth across the whole clip (no per-frame jumps)."""
    frames = max(1, int(round(duration * 30)))
    base   = ("scale=2560:1440:force_original_aspect_ratio=increase,"
              "crop=2560:1440,setsar=1")
    step   = 0.20 / frames   # total zoom travel of 0.20 over the clip
    move   = random.choice(["in", "out", "panr", "panl"])
    if move == "in":
        zp = (f"zoompan=z='min(1.0+on*{step:.7f},1.20)':d=1:"
              f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1280x720:fps=30")
    elif move == "out":
        zp = (f"zoompan=z='max(1.20-on*{step:.7f},1.0)':d=1:"
              f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1280x720:fps=30")
    elif move == "panr":
        zp = (f"zoompan=z=1.15:d=1:x='(iw-iw/zoom)*on/{frames}':"
              f"y='ih/2-(ih/zoom/2)':s=1280x720:fps=30")
    else:  # panl
        zp = (f"zoompan=z=1.15:d=1:x='(iw-iw/zoom)*(1-on/{frames})':"
              f"y='ih/2-(ih/zoom/2)':s=1280x720:fps=30")
    cmd = [FFMPEG,"-y","-loop","1","-i",src,"-t",f"{duration:.3f}",
           "-vf",f"{base},{zp}",
           "-r","30","-c:v","libx264","-preset","fast","-crf","23","-an", dst]
    return subprocess.run(cmd, capture_output=True).returncode == 0


def build_xfade_chain(seg_paths, output):
    """Chain all segments with slideleft xfade transitions."""
    n = len(seg_paths)
    if n == 0:
        return False
    if n == 1:
        import shutil; shutil.copy(seg_paths[0], output); return True

    inputs = []
    for p in seg_paths:
        inputs += ["-i", p]

    # Pre-compute all durations
    durations = [get_dur(p) for p in seg_paths]

    # xfade offset = cumulative sum of (dur - XFADE_DURATION) for all prior clips
    # This is the timestamp in the output stream where the next transition starts
    filter_parts = []
    cum_offset = 0.0
    for i in range(n - 1):
        in_a = "[0:v]" if i == 0 else f"[xf{i-1}]"
        in_b = f"[{i+1}:v]"
        out_label = f"[xf{i}]" if i < n - 2 else "[vout]"
        cum_offset += durations[i] - XFADE_DURATION
        filter_parts.append(
            f"{in_a}{in_b}xfade=transition=slideleft:"
            f"duration={XFADE_DURATION}:offset={cum_offset:.3f}{out_label}"
        )

    r = subprocess.run(
        [FFMPEG,"-y"] + inputs + [
            "-filter_complex", ";".join(filter_parts),
            "-map","[vout]",
            "-c:v","libx264","-preset","fast","-crf","21","-r","30",
            output],
        capture_output=True)
    if r.returncode != 0:
        print("XFADE ERROR:", r.stderr.decode()[-400:])
    return r.returncode == 0


def make_soft_whoosh(dst, duration=0.45):
    """Generate an airy soft whoosh SFX (filtered pink noise)."""
    cmd = [FFMPEG,"-y","-f","lavfi",
           "-i",f"anoisesrc=d={duration}:c=pink:a=0.8,afade=t=in:st=0:d=0.12,afade=t=out:st=0.2:d=0.25,highpass=f=300,volume=2.0",
           "-c:a","aac","-b:a","128k", dst]
    return subprocess.run(cmd, capture_output=True).returncode == 0


def build_whoosh_track(transition_times, total_dur, dst, volume=0.45):
    """Build a stereo audio track with soft whoosh at each transition timestamp."""
    whoosh_file = f"{WORK}/whoosh_single.aac"
    if not make_soft_whoosh(whoosh_file):
        return False

    audio_inputs = ["-f","lavfi","-i",
                    f"aevalsrc=0:s=44100:c=stereo:d={total_dur:.3f}"]
    afilt = ["[0:a]anull[base]"]
    for j, ts in enumerate(transition_times):
        ms = int(ts * 1000)
        audio_inputs += ["-i", whoosh_file]
        afilt.append(f"[{j+1}:a]adelay={ms}|{ms}[sw{j}]")
    mix_in = "[base]" + "".join(f"[sw{j}]" for j in range(len(transition_times)))
    afilt.append(
        f"{mix_in}amix=inputs={1+len(transition_times)}:normalize=0,"
        f"volume={volume}[aout]"
    )
    r = subprocess.run(
        [FFMPEG,"-y"] + audio_inputs + [
            "-filter_complex", ";".join(afilt),
            "-map","[aout]","-c:a","aac","-b:a","128k",
            "-t",f"{total_dur:.3f}", dst],
        capture_output=True)
    if r.returncode != 0:
        print("WHOOSH TRACK ERROR:", r.stderr.decode()[-300:])
    return r.returncode == 0

# ── CTA card generation ───────────────────────────────────────────────────────
FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

CTA_TEXT     = "Verpasse Keine Folge"
CTA_BTN_TEXT = "ABONNIEREN"

# CTA card dimensions — top-right sliding card
CTA_CARD_W   = 640
CTA_CARD_H   = 110
CTA_CARD_Y   = 20    # distance from top of frame
CTA_SLIDE_IN = 0.35  # seconds to slide in
CTA_HOLD     = 3.5   # seconds to hold (total ~4.2s fits within any 4-7s clip)
CTA_SLIDE_OUT= 0.35  # seconds to slide out

def make_cta_card_png():
    """Generate 640x110 dark translucent CTA card PNG."""
    png = f"{CTADIR}/cta_card.png"
    if os.path.exists(png):
        return png

    img = Image.new("RGBA", (CTA_CARD_W, CTA_CARD_H), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)

    # Dark translucent rounded background
    d.rounded_rectangle([0, 0, CTA_CARD_W-1, CTA_CARD_H-1],
                        radius=12, fill=(15, 15, 25, 200))

    # Gold accent line on left edge
    d.rounded_rectangle([0, 0, 5, CTA_CARD_H-1], radius=4, fill=(255, 180, 0, 255))

    font  = ImageFont.truetype(FONT_REG,  34)
    bfont = ImageFont.truetype(FONT_BOLD, 30)

    tbbox = d.textbbox((0,0), CTA_TEXT, font=font)
    tw, th = tbbox[2]-tbbox[0], tbbox[3]-tbbox[1]
    sbbox = d.textbbox((0,0), CTA_BTN_TEXT, font=bfont)
    stw, sth = sbbox[2]-sbbox[0], sbbox[3]-sbbox[1]
    btn_w = stw + 36
    btn_h = th + 12

    gap     = 20
    total_w = tw + gap + btn_w
    start_x = (CTA_CARD_W - total_w) // 2 + 8  # +8 to account for gold line
    cy      = CTA_CARD_H // 2

    # Text
    ty = cy - th // 2
    d.text((start_x+1, ty+1), CTA_TEXT, font=font, fill=(0,0,0,140))
    d.text((start_x,   ty),   CTA_TEXT, font=font, fill="white")

    # Red ABONNIEREN button
    bx = start_x + tw + gap
    by = cy - btn_h // 2
    d.rounded_rectangle([bx, by, bx+btn_w, by+btn_h], radius=7, fill=(220, 0, 0, 255))
    sx = bx + (btn_w - stw) // 2
    sy = by + (btn_h - sth) // 2
    d.text((sx, sy), CTA_BTN_TEXT, font=bfont, fill="white")

    img.save(png)
    return png

def apply_cta_overlay(src_seg, dst, duration=CTA_DURATION):
    """Burn sliding CTA card onto video — slides in from top-right, holds, slides out."""
    card_png = make_cta_card_png()

    # x animation: starts off-screen right (1280), slides to (1280-CTA_CARD_W-10)=630
    x_rest  = 1280 - CTA_CARD_W - 10   # resting x position (10px margin from right)
    x_off   = 1280                       # off-screen x position

    si = CTA_SLIDE_IN
    ho = CTA_SLIDE_IN + CTA_HOLD
    so = CTA_SLIDE_IN + CTA_HOLD + CTA_SLIDE_OUT

    # ffmpeg overlay x expression using if() for slide-in, hold, slide-out
    x_expr = (
        f"if(lt(t,{si}), {x_off}-({x_off}-{x_rest})*(t/{si}),"
        f" if(lt(t,{ho}), {x_rest},"
        f"  if(lt(t,{so}), {x_rest}+({x_off}-{x_rest})*((t-{ho})/{CTA_SLIDE_OUT}),"
        f"   {x_off})))"
    )

    r = subprocess.run([
        FFMPEG, "-y",
        "-i", src_seg,
        "-i", card_png,
        "-filter_complex",
        f"[0:v][1:v]overlay=x='{x_expr}':y={CTA_CARD_Y}:format=auto[out]",
        "-map", "[out]",
        "-r","30","-c:v","libx264","-preset","fast","-crf","20",
        "-an", dst
    ], capture_output=True)
    if r.returncode != 0:
        print(f"  CTA overlay error: {r.stderr.decode()[-150:]}")
        import shutil; shutil.copy(src_seg, dst)
    return os.path.exists(dst)

def make_fade_transition(seg_a, seg_b, dst, fade_dur=FADE_DUR):
    """Create a crossfade transition clip between two segments."""
    dur_a = get_dur(seg_a)
    frames = max(1, int(fade_dur * 30))
    offset = max(0, dur_a - fade_dur)
    r = subprocess.run([
        FFMPEG, "-y",
        "-i", seg_a, "-i", seg_b,
        "-filter_complex",
        (f"[0:v]trim=start={offset:.3f},setpts=PTS-STARTPTS[va];"
         f"[1:v]trim=end={fade_dur:.3f},setpts=PTS-STARTPTS[vb];"
         f"[va][vb]blend=all_expr='A*(1-T/{fade_dur})+B*(T/{fade_dur})'[out]"),
        "-map", "[out]",
        "-r","30","-c:v","libx264","-preset","fast","-crf","21",
        "-an", "-t", f"{fade_dur:.3f}", dst
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

# ── Scene plan — dynamic from script ─────────────────────────────────────────
TOTAL_DUR = get_dur(AUDIO)

def parse_script(script_path):
    """Parse script file (German or English) into list of (scene_text, word_count)."""
    scenes = []
    if not script_path or not os.path.exists(script_path):
        return scenes
    with open(script_path, encoding="utf-8") as f:
        content = f.read()
    # Support both [SZENE N] (German) and [SCENE N] (English)
    blocks = re.split(r'\[(?:SZENE|SCENE)\s+\d+[^\]]*\]', content, flags=re.IGNORECASE)
    for block in blocks[1:]:
        text = block.strip()
        if text:
            words = len(text.split())
            scenes.append((text, words))
    return scenes

def generate_scene_queries(scene_text, is_english=False):
    """Generate CLIP search queries. If English text, extract directly; else translate via Claude."""
    if not _anthropic_client:
        words = scene_text.split()[:8]
        desc = " ".join(words)
        return (desc, [desc])
    try:
        if is_english:
            prompt = f"""You are a stock video researcher for a YouTube channel. This is a scene from a travel/aviation video:
"{scene_text[:300]}"

Generate stock video search queries that are SPECIFIC and VARIED — avoid generic terms like "airplane cabin" or "airport terminal" unless the scene is literally about those. Instead, focus on the EMOTION, ACTION, or SPECIFIC SUBJECT of the scene.

Generate:
1. One short visual description (max 8 words) of the ideal stock clip — be specific and concrete
2. Four search queries (max 6 words each): make each one different in angle:
   - q1: the specific action or subject happening
   - q2: the emotion or human element
   - q3: a broader thematic term (profession, situation, context)
   - q4: a creative/metaphorical alternative that captures the mood

Reply in EXACTLY this format:
<desc>visual description here</desc>
<q1>search query 1</q1>
<q2>search query 2</q2>
<q3>search query 3</q3>
<q4>search query 4</q4>"""
        else:
            prompt = f"""You are a stock video researcher for a YouTube channel. This is a scene from a German travel/aviation video voiceover:
"{scene_text[:300]}"

Generate English stock video search queries that are SPECIFIC and VARIED — avoid generic terms like "airplane cabin" or "airport terminal" unless the scene is literally about those. Instead, focus on the EMOTION, ACTION, or SPECIFIC SUBJECT of the scene.

Generate:
1. One short English visual description (max 8 words) of the ideal stock clip — be specific and concrete
2. Four English search queries (max 6 words each): make each one different in angle:
   - q1: the specific action or subject happening
   - q2: the emotion or human element
   - q3: a broader thematic term (profession, situation, context)
   - q4: a creative/metaphorical alternative that captures the mood

Reply in EXACTLY this format:
<desc>visual description here</desc>
<q1>search query 1</q1>
<q2>search query 2</q2>
<q3>search query 3</q3>
<q4>search query 4</q4>"""

        msg = _anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text
        desc  = re.search(r'<desc>(.*?)</desc>', text)
        q1    = re.search(r'<q1>(.*?)</q1>', text)
        q2    = re.search(r'<q2>(.*?)</q2>', text)
        q3    = re.search(r'<q3>(.*?)</q3>', text)
        q4    = re.search(r'<q4>(.*?)</q4>', text)
        desc  = desc.group(1).strip() if desc else scene_text.split()[0]
        queries = [q.group(1).strip() for q in [q1, q2, q3, q4] if q]
        if not queries:
            queries = [desc]
        return (desc, queries)
    except Exception as e:
        print(f"  Query gen error: {e}")
        words = scene_text.split()[:6]
        desc = " ".join(words)
        return (desc, [desc])

# Load scripts — argv[3]=German script for timing, argv[4]=English script for queries
# If only one script passed, auto-detect language
_german_script_path = sys.argv[3] if len(sys.argv) > 3 else None
_english_script_path = sys.argv[4] if len(sys.argv) > 4 else None

_german_scenes  = parse_script(_german_script_path)  if _german_script_path  else []
_english_scenes = parse_script(_english_script_path) if _english_script_path else []

# Use German for word-count timing, English for query generation (if available)
_timing_scenes = _german_scenes if _german_scenes else _english_scenes
_query_scenes  = _english_scenes if _english_scenes else _german_scenes
_is_english    = bool(_english_scenes)

_scenes = _timing_scenes

if _scenes:
    total_words = sum(w for _, w in _scenes)
    RATE = TOTAL_DUR / max(total_words, 1)
    SCENE_WORDS = [w for _, w in _scenes]
    src_label = "German+English" if (_german_scenes and _english_scenes) else ("English" if _is_english else "German")
    print(f"Script loaded ({src_label}): {len(_scenes)} scenes, {total_words} words, rate={RATE:.3f}s/word")
    print("Generating scene queries via Claude…")
    SCENE_META = []
    for i, (scene_text, _) in enumerate(_query_scenes):
        desc, queries = generate_scene_queries(scene_text, is_english=_is_english)
        SCENE_META.append((desc, queries))
        print(f"  Scene {i+1:2d}: {desc[:60]}")
else:
    # Fallback: single scene covering full audio
    print("WARNING: No script found — using single generic scene")
    RATE = 1.0
    SCENE_WORDS = [int(TOTAL_DUR / 5.5)]
    SCENE_META = [("travel airport airplane flight", ["travel airport airplane flight", "airplane passenger travel", "airport terminal travel"])]

scene_starts = []
t = 0.0
# Inflate total to compensate for xfade duration loss between scenes
xfade_loss = (len(SCENE_WORDS) - 1) * XFADE_DURATION
INFLATED_DUR = TOTAL_DUR + xfade_loss
RATE_INF = INFLATED_DUR / max(sum(SCENE_WORDS), 1)
for w in SCENE_WORDS:
    scene_starts.append(round(t, 2))
    t += w * RATE_INF
scene_ends = scene_starts[1:] + [round(INFLATED_DUR, 2)]

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
source_counts = {}  # how many clips each source has contributed (for diversity)
session_used  = set()  # clip IDs used in THIS build (saved to history at end)
consecutive_images = 0  # running count of back-to-back Ken Burns image segments

for i, clip in enumerate(CLIPS):
    desc    = clip["desc"]
    queries = clip["queries"]
    dur     = clip["dur"]
    seg_p   = f"{SEGDIR}/s{i:04d}.mp4"

    # ── Check if CTA overlay should be applied to this clip ───────────────
    for ci, ct in enumerate(cta_times):
        if ci not in cta_inserted and abs(cum_t - ct) < dur:
            cta_inserted.add(ci)
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
    _sources = [
        lambda: search_pexels(queries, min_src, top_n=15),
        lambda: search_pixabay(queries, min_src, top_n=12),
        lambda: search_coverr(queries, min_src, top_n=10),
        lambda: search_mixkit(queries[:2], min_src, top_n=10),
        lambda: search_vecteezy(queries, min_src, top_n=10),
    ]
    random.shuffle(_sources)
    for _src in _sources:
        candidates += _src()

    def _try_candidates(pool, label=""):
        """Try downloading from pool in order; return (url, vid, src_dur, raw_p) or None."""
        for _url, _vid, _sdur, *_ in pool:
            if _vid in used_ids:
                continue
            _raw = f"{RAWDIR}/r{i:04d}_{_vid}.mp4"
            if download(_url, _raw):
                return _url, _vid, _sdur, _raw
            print(f"  DL retry failed{' ('+label+')' if label else ''}: {_vid}")
        return None

    url, vid, src_dur = best_candidate(candidates, desc,
                                       source_counts=source_counts, total_clips=len(seg_paths))

    # fallback: generic airport/travel clip
    if not url:
        _fallback_q = ["airport terminal travel","airplane flight travel","airport passengers travel"]
        _fb = []
        for _src in [lambda: search_pexels(_fallback_q, min_src, top_n=4),
                     lambda: search_pixabay(_fallback_q, min_src, top_n=4)]:
            _fb += _src()
            if _fb: break
        if _fb:
            candidates = _fb
            url, vid, src_dur = best_candidate(_fb, "airport travel")
            print(f"  → using generic airport fallback")

    if not url:
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
        # First choice failed — try remaining candidates before giving up
        print(f"[{i:3d}] ✗ download failed — trying alternatives…")
        used_ids.discard(vid)
        _remaining = [c for c in candidates if c[1] != vid and c[1] not in used_ids]
        _result = _try_candidates(_remaining, "alt")
        if _result:
            url, vid, src_dur, raw_p = _result
            used_ids.add(vid)
            print(f"  → recovered with alternative clip {vid}")
        else:
            print(f"[{i:3d}] ✗ all alternatives failed — black frame")
            subprocess.run([FFMPEG,"-y","-f","lavfi",
                            "-i",f"color=black:size=1280x720:rate=30:duration={dur:.3f}",
                            "-c:v","libx264","-preset","fast","-an", seg_p],
                           capture_output=True)
            timeline.append((seg_p, cum_t, cum_t+dur, clip["scene_last"], False))
            cum_t += dur
            seg_paths.append(seg_p); clip_scores.append(0)
            continue

    actual_dur = get_dur(raw_p) or src_dur

    # ── Step 2: best offset within clip ───────────────────────────────────
    best_t, best_s = best_offset(raw_p, desc, dur, actual_dur)

    # ── Image fallback: no good video match → try a Ken Burns still ────────
    # Only when the video score is poor AND we haven't already placed
    # MAX_CONSECUTIVE_IMAGES stills in a row (continuity rule, avoids boredom).
    is_first_clip = (i == 0)
    is_last_clip  = (i == len(CLIPS) - 1)
    if (best_s < IMAGE_FALLBACK_THRESHOLD
            and consecutive_images < MAX_CONSECUTIVE_IMAGES
            and not is_first_clip and not is_last_clip):
        img_cands = search_images(queries, top_n=12)
        scored = []
        for iurl, iid, ithumb in img_cands:
            scored.append((clip_score_url(ithumb, desc) if ithumb else 0.0, iurl, iid))
        scored.sort(reverse=True)
        if scored and scored[0][0] > best_s:
            img_s, img_url, img_id = scored[0]
            img_raw = f"{IMGDIR}/i{i:04d}_{img_id}.jpg"
            if download(img_url, img_raw) and make_image_segment(
                    img_raw, seg_p, dur, scene_last=clip["scene_last"]):
                if clip.get("cta_overlay"):
                    cta_out = seg_p.replace(".mp4","_cta.mp4")
                    if apply_cta_overlay(seg_p, cta_out, duration=dur):
                        seg_p = cta_out
                used_ids.add(img_id); session_used.add(img_id)
                consecutive_images += 1
                isrc = img_id.split("_")[0]
                print(f"[{i:3d}] [IMG {isrc}] scene={clip['scene']:2d}  "
                      f"{clip['start']:.1f}-{clip['end']:.1f}s  CLIP={img_s:.3f}  "
                      f"Ken Burns ({consecutive_images}/{MAX_CONSECUTIVE_IMAGES})  {queries[0][:30]}")
                timeline.append((seg_p, cum_t, cum_t+dur, clip["scene_last"], False))
                cum_t += dur
                seg_paths.append(seg_p); clip_scores.append(img_s)
                source_counts[isrc] = source_counts.get(isrc, 0) + 1
                time.sleep(0.15)
                continue

    # A video clip is being used → reset the consecutive-image streak.
    consecutive_images = 0

    # Ken Burns disabled — zoompan freezes video clips on first frame
    kb = False

    src_tag = vid.split("_")[0] if "_" in vid else "?"
    print(f"[{i:3d}] [{src_tag}] scene={clip['scene']:2d}  {clip['start']:.1f}-{clip['end']:.1f}s  "
          f"src={actual_dur:.0f}s  off={best_t:.1f}s  CLIP={best_s:.3f}  "
          f"KB={'Y' if kb else 'N'}  {queries[0][:35]}")

    ok = make_seg(raw_p, seg_p, dur, start_offset=best_t,
                  scene_last=clip["scene_last"], ken_burns=kb)

    if ok:
        # Apply CTA overlay if flagged
        if clip.get("cta_overlay"):
            cta_out = seg_p.replace(".mp4","_cta.mp4")
            if apply_cta_overlay(seg_p, cta_out, duration=dur):
                seg_p = cta_out
        timeline.append((seg_p, cum_t, cum_t+dur, clip["scene_last"], False))
        cum_t += dur
        seg_paths.append(seg_p); clip_scores.append(best_s)
        source_counts[src_tag] = source_counts.get(src_tag, 0) + 1
        session_used.add(vid)

        # ── B-roll cutaway every N clips ──────────────────────────────────
        if (i + 1) % BROLL_EVERY_N_CLIPS == 0:
            broll_queries = get_broll_queries(clip["desc"])
            if broll_queries:
                br_candidates = []
                br_candidates += search_pexels(broll_queries, BROLL_DURATION, top_n=6)
                br_candidates += search_pixabay(broll_queries, BROLL_DURATION, top_n=6)
                br_candidates += search_coverr(broll_queries, BROLL_DURATION, top_n=6)
                br_candidates += search_mixkit(broll_queries[:1], BROLL_DURATION, top_n=4)
                br_candidates += search_vecteezy(broll_queries, BROLL_DURATION, top_n=4)
                br_url, br_vid, br_src_dur = best_candidate(br_candidates, broll_queries[0])
                if br_url and br_vid not in used_ids:
                    used_ids.add(br_vid)
                    br_raw = f"{RAWDIR}/br{i:04d}_{br_vid}.mp4"
                    br_seg = f"{SEGDIR}/br{i:04d}.mp4"
                    if download(br_url, br_raw):
                        bd = get_dur(br_raw) or br_src_dur
                        bt, _ = best_offset(br_raw, broll_queries[0], BROLL_DURATION, bd)
                        if make_seg(br_raw, br_seg, BROLL_DURATION, start_offset=bt):
                            timeline.append((br_seg, cum_t, cum_t+BROLL_DURATION, False, False))
                            cum_t += BROLL_DURATION
                            print(f"  [B-roll: {broll_queries[0][:40]}]")
    else:
        print(f"  encode failed — filling with black frame")
        subprocess.run([FFMPEG,"-y","-f","lavfi",
                        "-i",f"color=black:size=1280x720:rate=30:duration={dur:.3f}",
                        "-c:v","libx264","-preset","fast","-an", seg_p],
                       capture_output=True)
        timeline.append((seg_p, cum_t, cum_t+dur, clip["scene_last"], False))
        cum_t += dur
        seg_paths.append(seg_p); clip_scores.append(0)

    time.sleep(0.15)

# Note: CTAs are overlaid on clips, not appended as separate segments

valid = [p for p,_,_,_,_ in timeline if p and os.path.exists(p)]
avg_s = sum(clip_scores[i] for i in range(len(clip_scores)) if clip_scores[i] != 99) / max(1, sum(1 for s in clip_scores if s != 99))
print(f"\nValid segments: {len(valid)}  avg CLIP score: {avg_s:.3f}")

# ── Group timeline by scene, hard-concat within each scene ───────────────────
# timeline entries: (seg_path, start_t, end_t, is_scene_last, is_cta)
# We use is_scene_last to detect scene boundaries.

valid_segs = [p for p,_,_,_,_ in timeline if p and os.path.exists(p)]
print(f"\nValid segments: {len(valid_segs)}  avg CLIP score: {avg_s:.3f}")

# Group segments by scene — each group gets hard-concat into one scene video
scene_groups = []   # list of lists of seg_paths
current_group = []
for seg_p, t_start, t_end, is_scene_last, is_cta in timeline:
    if not seg_p or not os.path.exists(seg_p):
        continue
    current_group.append(seg_p)
    if is_scene_last and current_group:
        scene_groups.append(current_group)
        current_group = []
if current_group:
    scene_groups.append(current_group)

print(f"Scenes: {len(scene_groups)} — hard cuts within, slideleft xfade between scenes")

# Hard-concat each scene group into a single scene video
scene_videos = []
for si, group in enumerate(scene_groups):
    scene_out = f"{WORK}/scene_{si:03d}.mp4"
    if len(group) == 1:
        import shutil; shutil.copy(group[0], scene_out)
    else:
        concat_f = f"{WORK}/scene_{si:03d}_concat.txt"
        with open(concat_f, "w") as f:
            for p in group:
                f.write(f"file '{p}'\n")
        r = subprocess.run([FFMPEG,"-y","-f","concat","-safe","0","-i",concat_f,
                            "-c:v","libx264","-preset","fast","-crf","21","-an",
                            scene_out], capture_output=True)
        if r.returncode != 0:
            print(f"  scene {si} concat error: {r.stderr.decode()[-200:]}")
            continue
    scene_videos.append(scene_out)

# ── Build xfade chain ONLY between scene videos ───────────────────────────────
combined = f"{WORK}/combined.mp4"
print(f"Building xfade chain for {len(scene_videos)} scenes…")
if not build_xfade_chain(scene_videos, combined):
    sys.exit(1)

# Transition timestamps = at each scene boundary (for whoosh SFX)
transition_times = []
acc = 0.0
for si, sv in enumerate(scene_videos[:-1]):
    d = get_dur(sv)
    transition_times.append(acc + d - XFADE_DURATION)
    acc += d - XFADE_DURATION

vid_dur = get_dur(combined)
print(f"Video duration: {int(vid_dur//60)}m{int(vid_dur%60):02d}s")

# ── Build soft whoosh SFX track ───────────────────────────────────────────────
whoosh_track = f"{WORK}/whoosh_track.aac"
print(f"Building soft whoosh track ({len(transition_times)} transitions)…")
has_whoosh = build_whoosh_track(transition_times, vid_dur, whoosh_track)

# ── Mix voiceover + whoosh SFX ────────────────────────────────────────────────
if has_whoosh:
    mixed = f"{WORK}/mixed.aac"
    r = subprocess.run([FFMPEG,"-y",
                        "-i", AUDIO, "-i", whoosh_track,
                        "-filter_complex",
                        "[0:a]volume=1.8[v0];[v0][1:a]amix=inputs=2:normalize=0[aout]",
                        "-map","[aout]","-c:a","aac","-b:a","128k", mixed],
                       capture_output=True)
    if r.returncode != 0:
        print("AUDIO MIX ERROR — using voiceover only")
        mixed = AUDIO
else:
    mixed = AUDIO

# ── Final mux with end fade-to-black ─────────────────────────────────────────
# Use audio duration as the master length — trim video to match
print(f"Muxing → {OUTPUT}")
audio_dur = get_dur(AUDIO)
fade_out_start = max(0, audio_dur - 1.5)
# If video shorter than audio, freeze last frame to fill the gap
vfilt = f"[0:v]tpad=stop_mode=clone:stop_duration={max(0, audio_dur - vid_dur + 2):.3f},trim=end={audio_dur:.3f},setpts=PTS-STARTPTS,fade=out:st={fade_out_start:.3f}:d=1.5[vout]"
r = subprocess.run([FFMPEG,"-y",
                    "-i", combined, "-i", mixed,
                    "-filter_complex",
                    (f"{vfilt};"
                     f"[1:a]afade=t=out:st={fade_out_start:.3f}:d=1.5[aout]"),
                    "-map","[vout]","-map","[aout]",
                    "-c:v","libx264","-preset","fast","-crf","21",
                    "-c:a","aac","-b:a","128k",
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
print(f"   Sources  : " + ", ".join(f"{k}={v}" for k, v in sorted(source_counts.items(), key=lambda x: -x[1])))

# Record this build's clip IDs with the current build number; they'll be
# blocked from the next DEDUP_COOLDOWN videos, then become reusable again.
for _cid in session_used:
    _clip_history[_cid] = BUILD_NO
_save_history(_clip_history, BUILD_NO)
print(f"   Recorded {len(session_used)} clip IDs for build #{BUILD_NO} "
      f"(reusable again after {DEDUP_COOLDOWN} builds)")

# ── Push-to-GitHub size: compress only if over 90MB, keep quality high ───────
if size > 90:
    compressed = OUTPUT.replace(".mp4","_gh.mp4")
    print(f"\nCompressing for GitHub ({size:.0f}MB > 90MB)…")
    subprocess.run([FFMPEG,"-y","-i",OUTPUT,
                    "-c:v","libx264","-crf","28","-preset","medium",
                    "-c:a","aac","-b:a","128k", compressed], capture_output=True)
    c_size = os.path.getsize(compressed)/1024/1024
    print(f"   GitHub copy: {c_size:.1f} MB → {compressed}")
else:
    compressed = OUTPUT
    print(f"   Size OK for GitHub ({size:.1f}MB) — no compression needed")
