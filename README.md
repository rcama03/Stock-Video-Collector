# Video Scraper

![Version](https://img.shields.io/badge/version-0.7.1-blue)
![Python](https://img.shields.io/badge/Python-3.9+-3776AB?logo=python&logoColor=white)
![PyQt6](https://img.shields.io/badge/PyQt6-GUI-41CD52?logo=qt&logoColor=white)
![Playwright](https://img.shields.io/badge/Playwright-Headless_Browser-2EAD33?logo=playwright&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-FTS5-003B57?logo=sqlite&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

> Headless browser crawler with a dark-themed PyQt6 desktop GUI for discovering, cataloging, and downloading stock video clips from multiple sites — with full metadata extraction, FTS5 keyword search, and a concurrent download manager.

![Screenshot](screenshot.png)

---

## Quick Start

```bash
git clone https://github.com/SysAdminDoc/VideoScraper.git
cd VideoScraper
python artlist_scraper.py  # Auto-installs all dependencies on first run
```

That's it. The script bootstraps everything automatically:

1. Installs Python packages (`PyQt6`, `playwright`, etc.)
2. Downloads Chromium via Playwright
3. Launches the GUI

> **Requirements:** Python 3.9+ — no other prerequisites. Works on Windows, Linux, and macOS.

---

## Features

### Multi-Site Crawling

| Site | Video Types | Metadata | Pagination |
|------|-------------|----------|------------|
| **Artlist** | M3U8 HLS streams | Clip ID, resolution, duration, FPS, camera, formats, creator, collection, tags | Infinite scroll |
| **Pexels** | MP4 direct (SD/HD/UHD via Canva CDN) | OpenGraph + JSON-LD, URL slug titles | Load More button (up to 15 clicks) |
| **Pixabay** | MP4, WebM | OpenGraph + JSON-LD | Infinite scroll |
| **Storyblocks** | M3U8, MP4, WebM | OpenGraph + JSON-LD | Infinite scroll |
| **Generic** | M3U8, MP4, WebM, DASH, MOV | Auto-detect (OG, JSON-LD, DOM) | Infinite scroll |

The **Generic** profile works on any site — it intercepts all video network requests and extracts whatever metadata is available.

### Browser Automation & Anti-Detection

| Feature | Description |
|---------|-------------|
| Stealth mode | Hides `navigator.webdriver` flag, spoofs plugin array and WebGL vendor/renderer |
| Challenge detection | Auto-detects Cloudflare, CAPTCHA, and challenge pages |
| Manual solve mode | Switches to visible browser for CAPTCHA solving, resumes automatically on clearance |
| Persistent profile | Browser session cookies, localStorage, and tokens persist across runs |
| Request interception | Blocks heavy HLS `.ts` segments during crawl to save bandwidth |
| Configurable delays | Page delay, scroll delay, M3U8 wait, timeout — all adjustable per-run |

### Video Discovery

The crawler uses four complementary strategies to find video URLs on every page:

```
┌───────────────────────────────────────────────────────────────────┐
│                         Page Load                                 │
├───────────────┬─────────────────┬─────────────┬───────────────────┤
│  XHR/Fetch    │   DOM Observer  │  Response   │   HTML Regex      │
│  Intercept    │   (MutationObs) │  Body Scan  │   Fallback        │
│               │                 │             │                   │
│  Hooks into   │  Watches for    │  Scans all  │  Regex sweep for  │
│  XMLHttpReq & │  <video src>    │  HTTP resp  │  M3U8/MP4/WebM    │
│  fetch() API  │  injections     │  bodies     │  + Canva partner  │
│               │                 │             │  links (Pexels)   │
└───────┬───────┴────────┬────────┴──────┬──────┴────────┬──────────┘
        │                │               │               │
        └────────────────┴───────┬───────┴───────────────┘
                                 ▼
                    ┌─────────────────────┐
                    │  Quality Comparison  │
                    │  UHD > HD > SD       │
                    │  Dedup by clip ID    │
                    └──────────┬──────────┘
                               ▼
                    ┌─────────────────────┐
                    │   SQLite Database    │
                    │   + FTS5 Index       │
                    └─────────────────────┘
```

### Database & Search

| Feature | Description |
|---------|-------------|
| SQLite with WAL mode | Concurrent reads, crash-safe writes |
| FTS5 full-text search | Search across title, creator, collection, tags, resolution, camera, duration |
| AND/OR search modes | Toggle between inclusive and exclusive multi-term search |
| Column filters | Filter by source site, resolution, creator, collection — all combinable with text search |
| Duration filter | Quick filter by clip length range |
| Saved searches | Save and recall frequent search + filter combos |
| FTS index rebuild | One-click repair if search results drift out of sync |

### Asset Management

| Feature | Description |
|---------|-------------|
| Star ratings | 1–5 star rating per clip |
| Favorites | Quick-toggle favorite flag for any clip |
| Notes | Free-text notes per clip |
| User tags | Custom tag system independent of source tags |
| Collections | Organize clips into named collections with color coding |
| Bulk operations | Context menu actions on any card in the grid |

### Download Manager

| Feature | Description |
|---------|-------------|
| Concurrent downloads | Configurable parallel download workers (default: 2) |
| ffmpeg HLS→MP4 | Automatic M3U8-to-MP4 conversion via ffmpeg |
| Retry with backoff | Exponential backoff retry (configurable max attempts) |
| Speed & ETA tracking | Real-time download speed and estimated completion time |
| Bandwidth limiting | Optional download speed cap |
| Filename templates | Customizable output filenames: `{title}`, `{clip_id}`, `{creator}`, `{collection}`, `{resolution}` |
| Sidecar metadata | JSON metadata file written alongside each downloaded MP4 |
| Thumbnail extraction | Auto-extracts a thumbnail frame from downloaded videos |

### Export Formats

| Format | Contents |
|--------|----------|
| `.txt` | Plain list of M3U8/MP4 URLs |
| `.json` | Full metadata for all clips (title, creator, tags, URLs, timestamps) |
| `.m3u` | Media player playlist — uses local path if downloaded, M3U8 URL otherwise |
| `.csv` | Spreadsheet-ready with all metadata columns |
| **Batch** | Export all four formats at once |

### GUI

| Feature | Description |
|---------|-------------|
| Dark theme | Catppuccin-inspired deep dark palette |
| Card grid view | Visual thumbnail grid with configurable card sizes (S/M/L) |
| Hover video preview | Mouse-over any card to preview the video inline |
| Detail panel | Always-visible side panel with full metadata, ratings, notes, tags, collections |
| System tray | Minimize to tray, continue crawling/downloading in background |
| Toast notifications | Non-blocking status notifications |
| Live crawl log | Real-time scrolling log with verbose/quiet toggle |
| Clipboard monitor | Opt-in URL detection from clipboard (auto-fills crawl URL input) |

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+F` | Focus search bar |
| `F5` | Refresh search results |
| `Ctrl+1` through `Ctrl+6` | Switch between tabs |

---

## Usage

### Basic Workflow

1. **Select a site profile** — check one or more profiles in the Crawl tab (Artlist, Pexels, Pixabay, Storyblocks, or Generic)
2. **Set the start URL** — auto-populated per profile, or paste any URL for Generic mode
3. **Configure crawl settings** — batch size, depth, delays, headless mode
4. **Start crawling** — the crawler discovers pages, extracts metadata, and intercepts video URLs
5. **Browse results** — switch to the Library tab to search, filter, rate, tag, and organize clips
6. **Download** — select clips and download with the built-in manager, or export URL lists for external tools

### Configuration

All settings persist automatically in a JSON config file. Key options:

| Setting | Default | Description |
|---------|---------|-------------|
| Batch size | 50 | Pages per crawl batch |
| Page delay | 2s | Wait between page loads |
| Scroll delay | 1s | Wait between scroll steps |
| M3U8 wait | 5s | Time to wait for video URLs to appear |
| Scroll steps | 10 | Number of scroll-down actions per page |
| Timeout | 30s | Page load timeout |
| Max pages | 0 (unlimited) | Stop after N pages |
| Max depth | 3 | Link-following depth |
| Headless | On | Run browser without visible window |
| Concurrent DLs | 2 | Parallel download workers |
| Max retries | 3 | Download retry attempts |
| Bandwidth limit | 0 (unlimited) | Download speed cap in KB/s |
| Clipboard monitor | Off | Auto-detect URLs from clipboard |

### Filename Templates

Customize download filenames using template variables:

```
{title}                      → Beautiful_Sunset.mp4
{clip_id}_{title}            → abc123_Beautiful_Sunset.mp4
{creator}/{collection}/{title} → JohnDoe/Nature/Beautiful_Sunset.mp4
```

Available variables: `{title}`, `{clip_id}`, `{creator}`, `{collection}`, `{resolution}`

---

## How It Works

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            PyQt6 GUI                                    │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
│  │  Crawl   │  │  Library  │  │  Detail   │  │ Download │  │  Export  │ │
│  │  Tab     │  │  Tab      │  │  Panel    │  │  Tab     │  │  Tab    │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬────┘ │
└───────┼──────────────┼──────────────┼──────────────┼──────────────┼─────┘
        │              │              │              │              │
        ▼              ▼              ▼              ▼              ▼
┌──────────────┐  ┌──────────────────────────┐  ┌──────────────────────┐
│   Crawler    │  │      SQLite + FTS5       │  │   Download Worker    │
│   Worker     │──│                          │──│                      │
│  (QThread)   │  │  clips, crawl_queue,     │  │  ThreadPoolExecutor  │
│              │  │  crawled_pages,           │  │  + ffmpeg HLS→MP4   │
│  Playwright  │  │  collections,            │  │  + retry backoff     │
│  Chromium    │  │  saved_searches           │  │  + speed tracking    │
└──────────────┘  └──────────────────────────┘  └──────────────────────┘
```

**Crawler Worker** — Runs Playwright in an async event loop on a dedicated QThread. Navigates pages, injects JavaScript hooks for XHR/fetch/DOM video interception, extracts metadata via regex selectors + OpenGraph + JSON-LD, and manages the crawl queue with depth/priority.

**Database Layer** — Thread-safe SQLite with WAL mode and a dedicated `threading.Lock`. FTS5 external content table indexes title, creator, collection, tags, resolution, camera, and duration. Quality-aware M3U8 URL upgrades prefer UHD over HD over SD.

**Download Worker** — Persistent queue on a QThread with a `ThreadPoolExecutor` for concurrent downloads. Handles M3U8→MP4 conversion via ffmpeg, exponential backoff retry, real-time speed/ETA calculation, sidecar JSON metadata, and thumbnail extraction.

---

## Troubleshooting

**"Chromium not found"** — Click the "Install Browser" button on the Crawl tab. This runs `playwright install chromium` automatically.

**Search results seem wrong or incomplete** — Click the "🔄 Rebuild Index" button on the Crawl tab to rebuild the FTS5 search index from scratch.

**Bot challenge / CAPTCHA detected** — Uncheck "Headless" mode and restart the crawl. The browser will open visibly so you can solve the challenge manually. The crawler pauses and resumes automatically once the challenge clears.

**Downloads fail repeatedly** — Check that ffmpeg is installed and on your PATH. The scraper auto-detects ffmpeg in common locations, but if it can't find it, downloads that require HLS→MP4 conversion will fail.

**Clipboard monitor not working** — The clipboard monitor is opt-in. Enable it in your config by adding `"clipboard_monitor": true`, or toggle it programmatically. On Linux/Wayland, clipboard access may require additional permissions.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Contributing

Issues and PRs welcome. If you add support for a new site, submit it as a `SiteProfile.register()` block with documented selectors and test URLs.
