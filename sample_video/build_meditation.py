#!/usr/bin/env python3
"""
Meditation Video Builder — sources clips from ALL free stock sites in artlist_scraper.py.

Searches: Pexels, Pixabay, Coverr, Mixkit, Videvo, LifeOfVids
Features:
  - No human faces (excluded from search queries)
  - HD/UHD only (1080p+)
  - Clips trimmed to 12-15 seconds
  - 0.7x slow motion for dreamy feel
  - Cool blue/teal color grading
  - Crossfade transitions between clips
  - Free CC0 meditation music with volume fade in/out
  - Cleanup of all source clips after assembly

Usage:
    python3 build_meditation.py

Prerequisites:
    ffmpeg must be installed
"""

import json, os, random, re, subprocess, sys, urllib.request, urllib.parse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLIP_DIR = os.path.join(SCRIPT_DIR, 'clips')
OUTPUT = os.path.join(SCRIPT_DIR, 'meditation_sea.mp4')

# ── API Keys (free — sign up at each site to get your own) ──────────────
PEXELS_API_KEY = ''   # Get free key at: https://www.pexels.com/api/
PIXABAY_API_KEY = ''  # Get free key at: https://pixabay.com/api/docs/

# ── Clip settings ───────────────────────────────────────────────────────
CLIP_TRIM_MIN = 12    # minimum clip duration in seconds
CLIP_TRIM_MAX = 15    # maximum clip duration in seconds
SLOWMO_FACTOR = 0.7   # slow motion speed (0.7 = 70% speed, dreamy)
CROSSFADE_DUR = 1.5   # crossfade duration between clips in seconds
MUSIC_FADE_IN = 4.0   # music fade in duration at start
MUSIC_FADE_OUT = 5.0  # music fade out duration at end

# ── Search queries — nature/ocean only, no people ───────────────────────
SEARCH_QUERIES = [
    'ocean waves nature -people -person -face -woman -man',
    'sea sunset aerial -people -person -face',
    'underwater coral reef -people -diver -person',
    'beach waves drone -people -person -crowd',
    'ocean horizon sunrise -people -person',
    'calm sea surface -people -person -boat',
    'tropical lagoon blue water -people -person',
    'sea turtle underwater -people -person',
    'ocean jellyfish deep sea -people -person',
    'seashore rocks waves -people -person',
    'bioluminescent ocean night -people -person',
    'kelp forest underwater -people -person',
]

# Clean queries for APIs that don't support minus syntax
def clean_query(q):
    return re.sub(r'\s+-\w+', '', q).strip()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

# ── Source: Pexels (API — free key required) ────────────────────────────

def search_pexels(query, per_page=5):
    if not PEXELS_API_KEY:
        return []
    q = clean_query(query)
    url = f'https://api.pexels.com/videos/search?query={urllib.parse.quote(q)}&per_page={per_page}&orientation=landscape&size=large'
    req = urllib.request.Request(url, headers={'Authorization': PEXELS_API_KEY, **HEADERS})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            results = []
            for v in data.get('videos', []):
                best = None
                for f in sorted(v.get('video_files', []), key=lambda x: x.get('width', 0), reverse=True):
                    w = f.get('width', 0)
                    if w >= 1920 and f.get('quality') in ('hd', 'uhd', None):
                        best = f
                        break
                if not best:
                    for f in sorted(v.get('video_files', []), key=lambda x: x.get('width', 0), reverse=True):
                        if f.get('width', 0) >= 1080:
                            best = f
                            break
                if best and best.get('link'):
                    results.append({
                        'url': best['link'],
                        'id': f"pexels_{v.get('id', '')}",
                        'source': 'Pexels',
                        'duration': v.get('duration', 0),
                        'width': best.get('width', 0),
                    })
            return results
    except Exception as e:
        print(f"    [Pexels] Failed: {e}")
        return []

# ── Source: Pixabay (API — free key required) ───────────────────────────

def search_pixabay(query, per_page=5):
    if not PIXABAY_API_KEY:
        return []
    q = clean_query(query)
    params = urllib.parse.urlencode({
        'key': PIXABAY_API_KEY, 'q': q, 'video_type': 'film',
        'per_page': per_page, 'safesearch': 'true', 'order': 'popular',
        'min_width': 1920,
    })
    url = f'https://pixabay.com/api/videos/?{params}'
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            results = []
            for hit in data.get('hits', []):
                vids = hit.get('videos', {})
                chosen = vids.get('large', {})
                if not chosen.get('url'):
                    chosen = vids.get('medium', {})
                if chosen.get('url') and chosen.get('width', 0) >= 1080:
                    results.append({
                        'url': chosen['url'],
                        'id': f"pixabay_{hit.get('id', '')}",
                        'source': 'Pixabay',
                        'duration': hit.get('duration', 0),
                        'width': chosen.get('width', 0),
                    })
            return results
    except Exception as e:
        print(f"    [Pixabay] Failed: {e}")
        return []

# ── Source: Coverr (no API key — scrape search page) ───────────────────

def search_coverr(query):
    q = clean_query(query)
    url = f'https://coverr.co/s?q={urllib.parse.quote(q)}'
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
                'width': 1920,
            })
        return results
    except Exception as e:
        print(f"    [Coverr] Failed: {e}")
        return []

# ── Source: Mixkit (no API key — scrape search page) ───────────────────

def search_mixkit(query):
    q = clean_query(query)
    slug = q.replace(' ', '-')
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
                'width': 1920,
            })
            if len(results) >= 3:
                break
        return results
    except Exception as e:
        print(f"    [Mixkit] Failed: {e}")
        return []

# ── Source: Videvo (no API key — scrape search page) ───────────────────

def search_videvo(query):
    q = clean_query(query)
    url = f'https://www.videvo.net/search/{urllib.parse.quote(q)}/?media_type=video'
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='replace')
        mp4s = re.findall(r'(https?://[^"\'\s]+videvo[^"\'\s]*\.mp4)', html, re.IGNORECASE)
        results = []
        for i, mp4 in enumerate(mp4s[:3]):
            results.append({
                'url': mp4,
                'id': f"videvo_{i}_{clean_query(query).replace(' ','_')}",
                'source': 'Videvo',
                'duration': 0,
                'width': 1920,
            })
        return results
    except Exception as e:
        print(f"    [Videvo] Failed: {e}")
        return []

# ── Source: Life of Vids (no API key — scrape page) ────────────────────

def search_lifeofvids(query):
    q = clean_query(query)
    url = f'https://www.lifeofvids.com/?s={urllib.parse.quote(q)}'
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
                'id': f"lifeofvids_{len(results)}_{clean_query(query).replace(' ','_')}",
                'source': 'LifeOfVids',
                'duration': 0,
                'width': 1920,
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
    results = []
    for name, fn in ALL_SOURCES:
        try:
            hits = fn(query)
            results.extend(hits)
        except Exception:
            pass
    return results


# ── Download ───────────────────────────────────────────────────────────

def download_clip(url, path):
    if os.path.exists(path) and os.path.getsize(path) > 10000:
        print(f"    [SKIP] Already exists: {os.path.basename(path)}")
        return True
    print(f"    [DL] {os.path.basename(path)} ...")
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


def get_resolution(path):
    try:
        out = subprocess.check_output([
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_streams', path
        ], text=True)
        for s in json.loads(out).get('streams', []):
            if s.get('codec_type') == 'video':
                return s.get('width', 0), s.get('height', 0)
    except Exception:
        pass
    return 0, 0


# ── Generate meditation music using ffmpeg (sine wave ambient pad) ─────

def generate_ambient_music(output_path, duration):
    """Generate a soothing ambient drone/pad using layered sine waves."""
    print(f"  [MUSIC] Generating {duration:.0f}s ambient meditation track...")

    # Layer multiple sine waves for a rich ambient pad
    # Low drone + mid harmonics + high shimmer
    filter_complex = (
        # Base drone (deep low frequency)
        f'sine=frequency=80:duration={duration}:sample_rate=44100[drone];'
        f'sine=frequency=120:duration={duration}:sample_rate=44100[drone2];'
        # Mid harmonic layer
        f'sine=frequency=174:duration={duration}:sample_rate=44100[mid1];'
        f'sine=frequency=261:duration={duration}:sample_rate=44100[mid2];'
        # High shimmer
        f'sine=frequency=396:duration={duration}:sample_rate=44100[high1];'
        f'sine=frequency=528:duration={duration}:sample_rate=44100[high2];'
        # Mix layers with different volumes
        '[drone]volume=0.15[dv];'
        '[drone2]volume=0.12[d2v];'
        '[mid1]volume=0.08[m1v];'
        '[mid2]volume=0.06[m2v];'
        '[high1]volume=0.04[h1v];'
        '[high2]volume=0.03[h2v];'
        # Combine all layers
        '[dv][d2v][m1v][m2v][h1v][h2v]amix=inputs=6:duration=longest[mixed];'
        # Apply reverb-like effect with delays for spaciousness
        f'[mixed]aecho=0.8:0.7:40|80|120:0.3|0.2|0.1[reverbed];'
        # Fade in and fade out
        f'[reverbed]afade=t=in:st=0:d={MUSIC_FADE_IN},'
        f'afade=t=out:st={duration - MUSIC_FADE_OUT}:d={MUSIC_FADE_OUT}[out]'
    )

    subprocess.run([
        'ffmpeg', '-y',
        '-filter_complex', filter_complex,
        '-map', '[out]',
        '-c:a', 'aac', '-b:a', '192k',
        output_path
    ], capture_output=True)

    if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
        print(f"  [MUSIC] Generated: {os.path.basename(output_path)}")
        return True
    print("  [MUSIC] WARNING: Music generation failed, video will be silent")
    return False


# ── Video Assembly ─────────────────────────────────────────────────────

def build_video(clips, output):
    """
    Build meditation video with:
    - 12-15s trim per clip
    - 0.7x slow motion
    - Blue/teal color grading
    - Crossfade transitions
    - Ambient music with fade in/out
    - Cleanup after assembly
    """
    if not clips:
        print("[ERR] No clips to build.")
        return False

    # Step 1: Prepare each clip (trim, slow-mo, color grade, scale)
    prepared = []
    for i, clip_path in enumerate(clips):
        clip_dur = get_duration(clip_path)
        if clip_dur <= 0:
            continue

        prep_path = os.path.join(CLIP_DIR, f'seg_{i:02d}.mp4')
        trim_dur = random.uniform(CLIP_TRIM_MIN, CLIP_TRIM_MAX)

        # If clip is shorter than trim target, use full length
        if clip_dur < trim_dur:
            trim_dur = clip_dur

        # After slow-mo, the visual duration stretches by 1/SLOWMO_FACTOR
        visual_dur = trim_dur / SLOWMO_FACTOR

        # Video filter chain:
        # 1. Trim to 12-15s
        # 2. Scale to 1080p
        # 3. Slow motion (setpts for video)
        # 4. Blue/teal color grading (curves + hue shift)
        # 5. Set fps to 30
        vf = (
            f'scale=1920:1080:force_original_aspect_ratio=decrease,'
            f'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,'
            f'setpts={1/SLOWMO_FACTOR}*PTS,'
            f'fps=30,'
            # Blue/teal color grade: boost blues, reduce reds/greens slightly
            f'colorbalance=rs=-0.15:gs=-0.05:bs=0.2:rm=-0.1:gm=0.0:bm=0.15:rh=-0.08:gh=0.0:bh=0.12,'
            # Slight contrast + saturation boost for cinematic look
            f'eq=contrast=1.05:saturation=1.15:brightness=-0.03'
        )

        subprocess.run([
            'ffmpeg', '-y', '-i', clip_path,
            '-t', str(trim_dur),
            '-vf', vf,
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '22',
            '-an',
            '-movflags', '+faststart',
            prep_path
        ], capture_output=True)

        if os.path.exists(prep_path) and os.path.getsize(prep_path) > 1000:
            actual_dur = get_duration(prep_path)
            prepared.append(prep_path)
            print(f"  [PREP] seg_{i:02d}.mp4  ({actual_dur:.1f}s with slow-mo + color grade)")

    if len(prepared) < 2:
        print("[ERR] Not enough segments for crossfade.")
        return False

    # Step 2: Crossfade all clips together
    print(f"\n  [XFADE] Building crossfade transitions ({CROSSFADE_DUR}s each)...")
    xfade_output = os.path.join(CLIP_DIR, 'crossfaded.mp4')

    n = len(prepared)
    inputs = []
    for p in prepared:
        inputs.extend(['-i', p])

    # Build xfade filter chain
    # Each xfade merges two streams with a transition offset
    filter_parts = []
    durations = [get_duration(p) for p in prepared]

    if n == 2:
        offset = max(0, durations[0] - CROSSFADE_DUR)
        filter_parts.append(
            f'[0:v][1:v]xfade=transition=fade:duration={CROSSFADE_DUR}:offset={offset:.2f}[outv]'
        )
        map_label = '[outv]'
    else:
        # First pair
        offset = max(0, durations[0] - CROSSFADE_DUR)
        filter_parts.append(
            f'[0:v][1:v]xfade=transition=fade:duration={CROSSFADE_DUR}:offset={offset:.2f}[xf0]'
        )
        cumulative_dur = durations[0] + durations[1] - CROSSFADE_DUR

        for i in range(2, n):
            prev_label = f'xf{i-2}'
            offset = max(0, cumulative_dur - CROSSFADE_DUR)
            if i == n - 1:
                out_label = 'outv'
            else:
                out_label = f'xf{i-1}'
            filter_parts.append(
                f'[{prev_label}][{i}:v]xfade=transition=fade:duration={CROSSFADE_DUR}:offset={offset:.2f}[{out_label}]'
            )
            cumulative_dur = cumulative_dur + durations[i] - CROSSFADE_DUR

        map_label = '[outv]'

    filter_complex = ';'.join(filter_parts)

    result = subprocess.run([
        'ffmpeg', '-y', *inputs,
        '-filter_complex', filter_complex,
        '-map', map_label,
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '22',
        '-movflags', '+faststart',
        xfade_output
    ], capture_output=True, text=True)

    if not os.path.exists(xfade_output) or os.path.getsize(xfade_output) < 1000:
        print(f"  [XFADE] Crossfade failed, falling back to concat...")
        print(f"          stderr: {result.stderr[-500:] if result.stderr else 'none'}")
        # Fallback: simple concat without crossfade
        concat_list = os.path.join(CLIP_DIR, 'concat.txt')
        with open(concat_list, 'w') as f:
            for t in prepared:
                f.write(f"file '{os.path.abspath(t)}'\n")
        subprocess.run([
            'ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_list,
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '22',
            '-movflags', '+faststart',
            xfade_output
        ], capture_output=True)
        os.remove(concat_list)

    # Clean up prepared segments
    for t in prepared:
        os.remove(t)

    if not os.path.exists(xfade_output):
        print("[ERR] Video assembly failed.")
        return False

    video_dur = get_duration(xfade_output)
    print(f"  [XFADE] Crossfaded video: {video_dur:.1f}s")

    # Step 3: Generate ambient meditation music
    music_path = os.path.join(CLIP_DIR, 'ambient_music.m4a')
    has_music = generate_ambient_music(music_path, video_dur)

    # Step 4: Combine video + music
    if has_music:
        print("  [MIX] Combining video with meditation music...")
        subprocess.run([
            'ffmpeg', '-y',
            '-i', xfade_output,
            '-i', music_path,
            '-c:v', 'copy',
            '-c:a', 'aac', '-b:a', '192k',
            '-shortest',
            '-movflags', '+faststart',
            output
        ], capture_output=True)
        os.remove(music_path)
        os.remove(xfade_output)
    else:
        os.rename(xfade_output, output)

    if os.path.exists(output):
        dur = get_duration(output)
        size_mb = os.path.getsize(output) / (1024 * 1024)
        print(f"\n  [DONE] {output}")
        print(f"         Duration: {dur:.1f}s | Size: {size_mb:.1f} MB | 1920x1080 | 30fps")
        print(f"         Effects: {SLOWMO_FACTOR}x slow-mo, blue/teal grade, {CROSSFADE_DUR}s crossfades")

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

    active = []
    if PEXELS_API_KEY:
        active.append('Pexels')
    if PIXABAY_API_KEY:
        active.append('Pixabay')
    active.extend(['Coverr', 'Mixkit', 'Videvo', 'LifeOfVids'])

    print("=" * 60)
    print("  MEDITATION SEA VIDEO BUILDER")
    print("=" * 60)
    print(f"  Sources:    {', '.join(active)}")
    print(f"  Clip trim:  {CLIP_TRIM_MIN}-{CLIP_TRIM_MAX}s")
    print(f"  Slow-mo:    {SLOWMO_FACTOR}x speed")
    print(f"  Color:      Blue/teal cinematic grade")
    print(f"  Transitions: {CROSSFADE_DUR}s crossfades")
    print(f"  Music:      Ambient meditation (generated)")
    print(f"  Faces:      Excluded from search")
    if not PEXELS_API_KEY:
        print("  [INFO] Pexels skipped — set PEXELS_API_KEY (free at pexels.com/api/)")
    if not PIXABAY_API_KEY:
        print("  [INFO] Pixabay skipped — set PIXABAY_API_KEY (free at pixabay.com/api/docs/)")
    print("=" * 60)
    print()

    all_clips = []
    used_ids = set()

    for query in SEARCH_QUERIES:
        display_q = clean_query(query)
        print(f"[SEARCH] '{display_q}' across {len(active)} sources")
        results = search_all_sources(query)

        for v in results:
            if v['id'] in used_ids:
                continue
            clip_path = os.path.join(CLIP_DIR, f"{v['id']}.mp4")
            if download_clip(v['url'], clip_path):
                w, h = get_resolution(clip_path)
                if w < 1080 and h < 1080:
                    print(f"    [SKIP] {v['id']} — resolution too low ({w}x{h})")
                    os.remove(clip_path)
                    continue
                all_clips.append(clip_path)
                used_ids.add(v['id'])
                print(f"    [OK] {v['source']}: {v['id']} ({w}x{h})")
                break

        if len(all_clips) >= 10:
            break

    sources_used = len(set(r.split('_')[0] for r in used_ids))
    print(f"\n[INFO] Collected {len(all_clips)} HD+ clips from {sources_used} source(s)\n")

    if len(all_clips) < 3:
        print("[ERR] Not enough clips downloaded.")
        print("      Set API keys for Pexels/Pixabay, or check your internet.")
        sys.exit(1)

    print("[BUILD] Assembling meditation video...\n")
    ok = build_video(all_clips, OUTPUT)
    if not ok:
        print("[ERR] Video build failed.")
        sys.exit(1)

    print("\n=== Complete ===")


if __name__ == '__main__':
    main()
