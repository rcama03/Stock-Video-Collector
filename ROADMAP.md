# Stock Video Collector Roadmap

Roadmap for Stock Video Collector - a PyQt6 + Playwright desktop tool that crawls stock-video sites, indexes clips in SQLite+FTS5, and manages concurrent downloads.

## Planned Features

### Site coverage
- Vimeo stock (HLS + progressive MP4 extraction)
- Adobe Stock Video (public preview watermarked clips for research)
- Shutterstock, Envato Elements, Motion Array preview clips
- YouTube CC-BY collections (ingest via yt-dlp fallback, not Playwright)
- Coverr.co, Mazwai, Videvo, Mixkit, Videezy - royalty-free targets
- Generic RSS/Atom ingest for any site with a video feed

### Crawler hardening
- Playwright persistent context rotation + optional proxy pool (user-supplied proxies.txt)
- Residential-proxy toggle with per-session IP stickiness
- Human-in-the-loop CAPTCHA bypass with Discord/Telegram ping when a challenge appears
- Rate-limit respect: per-site `robots.txt` + `X-RateLimit` header observation
- Scheduled crawls (APScheduler) with nightly incremental runs
- Per-site plugin interface so new sites can be added without touching core

### Metadata & search
- Embedding-based semantic search (CLIP video embeddings) for "find a clip of a sunset" without tags
- Scene detection + thumbnail-mosaic preview per clip
- Tag co-occurrence graph for tag refinement and discovery
- Duration-bucketed browsing (<5s, 5-15s, 15-30s, >30s)
- Locked-collection view for portfolio curation
- Saved-search feeds with new-match notifications

### Download manager
- yt-dlp fallback for any URL the Playwright extractor misses
- Accurate resume on partial HLS downloads (verify segment count + total bytes)
- Queue priorities + per-site concurrency caps
- Bandwidth schedule (full-speed nights, throttle daytime)
- Post-download transcode presets (H.264 1080p proxy, HEVC 4K archive, ProRes master)

### Library & export
- Import existing MP4 libraries and reverse-index metadata via ffprobe
- Tag editor with bulk rename, merge, and split
- Export to Adobe Premiere / DaVinci Resolve bin (XML) with proxies pre-linked
- Export to OpenCut / Final Cut XML
- Batch ffmpeg watermark-stripper (for user's *own* uploaded clips that have preview watermarks)

## Competitive Research

- **yt-dlp / gallery-dl** - bulk extraction at CLI level, no library management. Stock Video Collector is the GUI + library layer on top; add a "yt-dlp URL list" import/export so power users can bridge.
- **Eagle / Billfish** - best-in-class visual library UX with pinch-zoom grids and smart folders; borrow their folder/tag hybrid model and batch tag ops.
- **Lightroom / Bridge** - rating + flag + color-label workflow; already partly mirrored with stars + favorites, add color labels and collection-as-query.
- **PornHub Downloader style tools** - aggressive anti-detection recipes (fingerprint randomization, canvas spoofing). The Playwright stealth implementation is close, but needs optional JS fingerprint rotation.

## Nice-to-Haves

- Built-in lightweight editor: trim + concatenate + export without leaving the app
- Automatic copyright check (frame-hash against a known-copyrighted db) with risk score
- Discord-bot companion that takes a search query and drops newest matches into a channel
- "Similar clips" from CLIP embedding nearest-neighbor search
- Project packaging - bundle selected clips into a zip with JSON metadata for handoff
- Watch-folder that auto-imports downloaded files from other tools
- Multi-machine library sync via SQLite replication or Litestream

## Open-Source Research (Round 2)

### Related OSS Projects
- https://github.com/Gabriellgpc/pexel-downloader — Pexels API + scrape hybrid, simple CLI MIT
- https://github.com/ToshY/pexels-scraper — Pexels image topic-search scraper, Python
- https://github.com/rpawk/image_downloader — Multi-source Pexels + Pixabay image downloader
- https://github.com/opheliabm/claude-videoedit — Pixabay stock-video pipeline: search -> download -> contrast-aware text overlay -> Remotion render
- https://github.com/topics/pexels-downloader — Topic hub
- https://github.com/topics/pixabay — Pixabay topic hub
- https://github.com/nficano/pytube — Reference for progress-hook + resumable download pattern (applicable to any HTTP video dl)
- https://github.com/mikf/gallery-dl — Extensible site-extractor architecture, excellent plugin model for adding new stock sites

### Features to Borrow
- Official-API-first, browser-scrape-fallback — Pexels and Pixabay have generous free APIs; hit API when key present, fall back to Playwright only for logged-in Artlist/Envato (search results note this as cleaner than scraping)
- Contrast-aware overlay pipeline from claude-videoedit — after download, auto-analyze clip for safe text regions, produce promo/marketing cut
- gallery-dl style extractor interface — each stock site = one extractor class with `pattern`, `extract_items`, `download_item`; drop-in extend
- Pytube-style progress hook (bytes_downloaded, total, speed) surfaced in PyQt6 progress delegate (already present; cross-reference for resumable HTTP range)
- Pixabay free-API key mode — 20k req/month free, avoid Playwright entirely for Pixabay
- "Similar clips" via CLIP embedding nearest-neighbor — already on roadmap; ref https://github.com/mlfoundations/open_clip

### Patterns & Architectures Worth Studying
- Extractor plugin pattern (gallery-dl) — `extractors/artlist.py`, `extractors/pexels.py`, each declaring URL patterns + cookie needs; core handles queuing, dedupe, rate-limit, retry
- Rate-limit decorator with per-host token bucket so concurrent downloads from same host respect ToS
- Resumable chunked download with SHA256 verify + `.part` file (pytube, gallery-dl, ffmpeg-dl all use this)
- Cookie-jar persistence per extractor so Artlist re-login doesn't happen every run — save to keyring, not plaintext
- FTS5 + embedding hybrid search — FTS for keywords, CLIP vector for visual similarity, union-rank with RRF
