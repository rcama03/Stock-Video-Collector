#!/usr/bin/env python3
"""
Meditation Video Builder — sources clips from ALL free stock sites in artlist_scraper.py.

Searches: Pexels, Pixabay, Coverr, Mixkit, Videvo, LifeOfVids
No paid API keys required — Pexels & Pixabay offer free keys, the rest are scraped directly.

Usage:
    python3 build_meditation.py

Prerequisites:
    pip install playwright && python -m playwright install chromium
    ffmpeg must be installed
"""

import json, os, re, subprocess, sys, time, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLIP_DIR = os.path.join(SCRIPT_DIR, 'clips')
OUTPUT = os.path.join(SCRIPT_DIR, 'meditation_sea_1min.mp4')

# ── API Keys (free — sign up at each site to get your own) ──────────────
PEXELS_API_KEY = ''   # Get free key at: https://www.pexels.com/api/
PIXABAY_API_KEY = ''  # Get free key at: https://pixabay.com/api/docs/

# ── Search queries — varied ocean/sea terms for meditation content ──────
SEARCH_QUERIES = [
    'ocean waves',
    'sea sunset',
    'underwater ocean',
    'beach aerial',
    'ocean horizon',
    'calm sea',
    'coral reef',
    'sea turtle',
    'tropical beach',
    'ocean drone',
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

# ── Source: Pexels (API — free key required) ────────────────────────────

def search_pexels(query, per_page=3):
    if not PEXELS_API_KEY:
        return []
    url = f'https://api.pexels.com/videos/search?query={urllib.parse.quote(query)}&per_page={per_page}&orientation=landscape&size=medium'
    req = urllib.request.Request(url, headers={'Authorization': PEXELS_API_KEY, **HEADERS})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            results = []
            for v in data.get('videos', []):
                best = None
                for f in v.get('video_files', []):
                    w = f.get('width', 0)
                    if f.get('quality') == 'hd' or (1280 <= w <= 1920):
                        best = f
                        break
                if not best:
                    files = sorted(v.get('video_files', []), key=lambda x: x.get('width', 0), reverse=True)
                    best = files[0] if files else None
                if best and best.get('link'):
                    results.append({
                        'url': best['link'],
                        'id': f"pexels_{v.get('id', '')}",
                        'source': 'Pexels',
                        'duration': v.get('duration', 0),
                    })
            return results
    except Exception as e:
        print(f"    [Pexels] Failed: {e}")
        return []

# ── Source: Pixabay (API — free key required) ───────────────────────────

def search_pixabay(query, per_page=3):
    if not PIXABAY_API_KEY:
        return []
    params = urllib.parse.urlencode({
        'key': PIXABAY_API_KEY, 'q': query, 'video_type': 'film',
        'per_page': per_page, 'safesearch': 'true', 'order': 'popular',
    })
    url = f'https://pixabay.com/api/videos/?{params}'
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            results = []
            for hit in data.get('hits', []):
                vids = hit.get('videos', {})
                chosen = vids.get('large', {}) or vids.get('medium', {}) or vids.get('small', {})
                if chosen.get('url'):
                    results.append({
                        'url': chosen['url'],
                        'id': f"pixabay_{hit.get('id', '')}",
                        'source': 'Pixabay',
                        'duration': hit.get('duration', 0),
                    })
            return results
    except Exception as e:
        print(f"    [Pixabay] Failed: {e}")
        return []

# ── Source: Coverr (no API key — scrape search page) ───────────────────

def search_coverr(query):
    url = f'https://coverr.co/s?q={urllib.parse.quote(query)}'
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='replace')
        mp4s = re.findall(r'(https?://[^"\'\s]+coverr[^"\'\s]*\.mp4)', html, re.IGNORECASE)
        slugs = re.findall(r'href="/footage/([^"]+)"', html)
        results = []
        for i, mp4 in enumerate(mp4s[:3]):
            results.append({
                'url': mp4,
                'id': f"coverr_{slugs[i] if i < len(slugs) else i}",
                'source': 'Coverr',
                'duration': 0,
            })
        return results
    except Exception as e:
        print(f"    [Coverr] Failed: {e}")
        return []

# ── Source: Mixkit (no API key — scrape search page) ───────────────────

def search_mixkit(query):
    slug = query.replace(' ', '-')
    url = f'https://mixkit.co/free-stock-video/{slug}/'
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='replace')
        mp4s = re.findall(r'(https?://assets\.mixkit\.co/[^"\'\s]+\.mp4)', html)
        seen = set()
        results = []
        for mp4 in mp4s:
            if mp4 in seen:
                continue
            seen.add(mp4)
            vid_id = re.search(r'/(\d+)/', mp4)
            results.append({
                'url': mp4,
                'id': f"mixkit_{vid_id.group(1) if vid_id else len(results)}",
                'source': 'Mixkit',
                'duration': 0,
            })
            if len(results) >= 3:
                break
        return results
    except Exception as e:
        print(f"    [Mixkit] Failed: {e}")
        return []

# ── Source: Videvo (no API key — scrape search page) ───────────────────

def search_videvo(query):
    url = f'https://www.videvo.net/search/{urllib.parse.quote(query)}/?media_type=video'
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='replace')
        mp4s = re.findall(r'(https?://[^"\'\s]+videvo[^"\'\s]*\.mp4)', html, re.IGNORECASE)
        results = []
        for i, mp4 in enumerate(mp4s[:3]):
            results.append({
                'url': mp4,
                'id': f"videvo_{i}",
                'source': 'Videvo',
                'duration': 0,
            })
        return results
    except Exception as e:
        print(f"    [Videvo] Failed: {e}")
        return []

# ── Source: Life of Vids (no API key — scrape page) ────────────────────

def search_lifeofvids(query):
    url = f'https://www.lifeofvids.com/?s={urllib.parse.quote(query)}'
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='replace')
        mp4s = re.findall(r'(https?://[^"\'\s]+\.mp4)', html)
        results = []
        seen = set()
        for mp4 in mp4s:
            if mp4 in seen:
                continue
            seen.add(mp4)
            results.append({
                'url': mp4,
                'id': f"lifeofvids_{len(results)}",
                'source': 'LifeOfVids',
                'duration': 0,
            })
            if len(results) >= 3:
                break
        return results
    except Exception as e:
        print(f"    [LifeOfVids] Failed: {e}")
        return []


# ── All sources combined ───────────────────────────────────────────────

ALL_SOURCES = [
    ('Pexels',     search_pexels),
    ('Pixabay',    search_pixabay),
    ('Coverr',     search_coverr),
    ('Mixkit',     search_mixkit),
    ('Videvo',     search_videvo),
    ('LifeOfVids', search_lifeofvids),
]


def search_all_sources(query):
    """Search all free stock video sources for a query, return combined results."""
    results = []
    for name, fn in ALL_SOURCES:
        try:
            hits = fn(query)
            results.extend(hits)
        except Exception:
            pass
    return results


# ── Download & Video Assembly ──────────────────────────────────────────

def download_clip(url, path):
    if os.path.exists(path) and os.path.getsize(path) > 10000:
        print(f"    [SKIP] Already exists: {os.path.basename(path)}")
        return True
    print(f"    [DL] {os.path.basename(path)} from {url[:60]}...")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(path, 'wb') as f:
                while True:
                    chunk = resp.read(1024 * 64)
                    if not chunk:
                        break
                    f.write(chunk)
        if os.path.getsize(path) < 5000:
            os.remove(path)
            return False
        return True
    except Exception as e:
        print(f"    [ERR] Download failed: {e}")
        if os.path.exists(path):
            os.remove(path)
        return False


def get_duration(path):
    try:
        out = subprocess.check_output([
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', path
        ], text=True)
        return float(json.loads(out)['format']['duration'])
    except Exception:
        return 0


def build_video(clips, output):
    """Scale all clips to 1080p/30fps, add fade transitions, concatenate — full length, no trimming."""
    prepared = []
    for i, clip_path in enumerate(clips):
        clip_dur = get_duration(clip_path)
        if clip_dur <= 0:
            continue
        prep_path = os.path.join(CLIP_DIR, f'seg_{i:02d}.mp4')

        fade_dur = 0.8
        subprocess.run([
            'ffmpeg', '-y', '-i', clip_path,
            '-vf', (
                f'scale=1920:1080:force_original_aspect_ratio=decrease,'
                f'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,'
                f'fade=t=in:st=0:d={fade_dur},'
                f'fade=t=out:st={max(0, clip_dur - fade_dur)}:d={fade_dur}'
            ),
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
            '-an', '-movflags', '+faststart',
            prep_path
        ], capture_output=True)

        if os.path.exists(prep_path) and os.path.getsize(prep_path) > 1000:
            actual_dur = get_duration(prep_path)
            prepared.append(prep_path)
            print(f"  [PREP] seg_{i:02d}.mp4  ({actual_dur:.1f}s — full length)")

    if not prepared:
        print("[ERR] No segments produced.")
        return False

    concat_list = os.path.join(CLIP_DIR, 'concat.txt')
    with open(concat_list, 'w') as f:
        for t in prepared:
            f.write(f"file '{os.path.abspath(t)}'\n")

    subprocess.run([
        'ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_list,
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '22',
        '-movflags', '+faststart',
        output
    ], capture_output=True)

    for t in prepared:
        os.remove(t)
    os.remove(concat_list)

    if os.path.exists(output):
        dur = get_duration(output)
        size_mb = os.path.getsize(output) / (1024 * 1024)
        print(f"\n  [DONE] {output}")
        print(f"         Duration: {dur:.1f}s | Size: {size_mb:.1f} MB | Resolution: 1920x1080")

        # Clean up original downloaded clips
        for c in clips:
            if os.path.exists(c):
                os.remove(c)
        if os.path.isdir(CLIP_DIR) and not os.listdir(CLIP_DIR):
            os.rmdir(CLIP_DIR)
        print("  [CLEANUP] Removed all source clips")

        return True
    return False


def main():
    os.makedirs(CLIP_DIR, exist_ok=True)

    # Show which sources are active
    active = []
    if PEXELS_API_KEY:
        active.append('Pexels')
    if PIXABAY_API_KEY:
        active.append('Pixabay')
    active.extend(['Coverr', 'Mixkit', 'Videvo', 'LifeOfVids'])

    print("=== Meditation Sea Video Builder ===")
    print(f"Sources: {', '.join(active)}")
    if not PEXELS_API_KEY:
        print("  [INFO] Pexels API key not set — skipping Pexels (get free key at pexels.com/api/)")
    if not PIXABAY_API_KEY:
        print("  [INFO] Pixabay API key not set — skipping Pixabay (get free key at pixabay.com/api/docs/)")
    print()

    all_clips = []
    used_ids = set()

    for query in SEARCH_QUERIES:
        print(f"[SEARCH] '{query}' across {len(active)} sources")
        results = search_all_sources(query)

        for v in results:
            if v['id'] in used_ids:
                continue
            clip_path = os.path.join(CLIP_DIR, f"{v['id']}.mp4")
            if download_clip(v['url'], clip_path):
                all_clips.append(clip_path)
                used_ids.add(v['id'])
                print(f"    [OK] {v['source']}: {v['id']}")
                break  # one clip per query per round

        if len(all_clips) >= 10:
            break

    print(f"\n[INFO] Collected {len(all_clips)} clips from {len(set(r.split('_')[0] for r in used_ids))} sources\n")

    if len(all_clips) < 3:
        print("[ERR] Not enough clips downloaded.")
        print("      Make sure you have API keys set for Pexels/Pixabay,")
        print("      or check your internet connection for other sources.")
        sys.exit(1)

    print("[BUILD] Assembling meditation video (full-length clips)...\n")
    ok = build_video(all_clips, OUTPUT)
    if not ok:
        print("[ERR] Video build failed.")
        sys.exit(1)

    print("\n=== Complete ===")


if __name__ == '__main__':
    main()
