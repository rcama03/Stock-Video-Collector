#!/usr/bin/env python3
"""Download free sea/ocean clips from Pixabay and assemble a 1-minute meditation video."""

import json, os, subprocess, sys, urllib.request, urllib.parse

CLIP_DIR = os.path.join(os.path.dirname(__file__), 'clips')
OUTPUT = os.path.join(os.path.dirname(__file__), 'meditation_sea_1min.mp4')
TARGET_DURATION = 60

PIXABAY_API_KEY = '47403938-c37e3c3a8f6b0c1b07f8a5b89'

SEARCH_QUERIES = [
    'ocean waves',
    'sea sunset',
    'underwater ocean',
    'beach aerial',
    'ocean horizon',
    'calm sea',
    'coral reef',
    'sea turtle',
]


def fetch_pixabay_videos(query, per_page=3):
    """Fetch videos from Pixabay API."""
    params = urllib.parse.urlencode({
        'key': PIXABAY_API_KEY,
        'q': query,
        'video_type': 'film',
        'per_page': per_page,
        'safesearch': 'true',
        'order': 'popular',
    })
    url = f'https://pixabay.com/api/videos/?{params}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            videos = []
            for hit in data.get('hits', []):
                vids = hit.get('videos', {})
                # Prefer 'large' (1920x1080) then 'medium' (1280x720)
                chosen = vids.get('large', {}) or vids.get('medium', {}) or vids.get('small', {})
                if chosen.get('url'):
                    videos.append({
                        'url': chosen['url'],
                        'width': chosen.get('width', 0),
                        'height': chosen.get('height', 0),
                        'duration': hit.get('duration', 10),
                        'id': hit.get('id', ''),
                    })
            return videos
    except Exception as e:
        print(f"  [WARN] Pixabay API failed for '{query}': {e}")
        return []


def download_clip(url, path):
    if os.path.exists(path) and os.path.getsize(path) > 10000:
        print(f"  [SKIP] Already exists: {os.path.basename(path)}")
        return True
    print(f"  [DL] {os.path.basename(path)} ...")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
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
        print(f"  [ERR] Download failed: {e}")
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


def build_video(clips, output, target_dur=60):
    total_clips = len(clips)
    segment_dur = target_dur / total_clips

    trimmed = []
    for i, clip_path in enumerate(clips):
        clip_dur = get_duration(clip_path)
        if clip_dur <= 0:
            continue
        trim_to = min(segment_dur, clip_dur)
        trimmed_path = os.path.join(CLIP_DIR, f'seg_{i:02d}.mp4')

        fade_dur = 0.8
        subprocess.run([
            'ffmpeg', '-y', '-i', clip_path,
            '-t', str(trim_to),
            '-vf', (
                f'scale=1920:1080:force_original_aspect_ratio=decrease,'
                f'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,'
                f'fade=t=in:st=0:d={fade_dur},fade=t=out:st={max(0, trim_to - fade_dur)}:d={fade_dur}'
            ),
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
            '-an', '-movflags', '+faststart',
            trimmed_path
        ], capture_output=True)

        if os.path.exists(trimmed_path) and os.path.getsize(trimmed_path) > 1000:
            actual_dur = get_duration(trimmed_path)
            trimmed.append(trimmed_path)
            print(f"  [TRIM] seg_{i:02d}.mp4  ({actual_dur:.1f}s)")

    if not trimmed:
        print("[ERR] No trimmed segments produced.")
        return False

    concat_list = os.path.join(CLIP_DIR, 'concat.txt')
    with open(concat_list, 'w') as f:
        for t in trimmed:
            f.write(f"file '{os.path.abspath(t)}'\n")

    subprocess.run([
        'ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_list,
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '22',
        '-movflags', '+faststart',
        output
    ], capture_output=True)

    for t in trimmed:
        os.remove(t)
    os.remove(concat_list)

    if os.path.exists(output):
        dur = get_duration(output)
        size_mb = os.path.getsize(output) / (1024 * 1024)
        print(f"\n  [DONE] {output}")
        print(f"         Duration: {dur:.1f}s | Size: {size_mb:.1f} MB | Resolution: 1920x1080")
        return True
    return False


def main():
    os.makedirs(CLIP_DIR, exist_ok=True)
    print("=== Meditation Sea Video Builder ===\n")

    all_clips = []
    for query in SEARCH_QUERIES:
        print(f"[SEARCH] '{query}'")
        videos = fetch_pixabay_videos(query, per_page=3)
        if not videos:
            continue
        v = videos[0]
        clip_path = os.path.join(CLIP_DIR, f"pixabay_{v['id']}.mp4")
        if download_clip(v['url'], clip_path):
            all_clips.append(clip_path)
        if len(all_clips) >= 8:
            break

    print(f"\n[INFO] Downloaded {len(all_clips)} clips\n")

    if len(all_clips) < 3:
        print("[ERR] Not enough clips. Check network access.")
        sys.exit(1)

    print("[BUILD] Assembling 1-minute meditation video...\n")
    ok = build_video(all_clips, OUTPUT, TARGET_DURATION)
    if not ok:
        print("[ERR] Video build failed.")
        sys.exit(1)

    print("\n=== Complete ===")


if __name__ == '__main__':
    main()
