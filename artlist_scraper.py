# Video Scraper v0.7.1
# Headless crawler with PyQt6 GUI — full metadata + keyword search + download manager
# Phase 1-3: Search quality, thumbnail pipeline, card/grid view
# Phase 4-6: Detail panel, archive management, collections, stats, filename templates
# v0.2.1: Bug fixes, performance optimizations, WAL mode, bounded logs, debounced UI
# v0.3.0: Context menus, keyboard shortcuts, system tray, concurrent downloads,
#          retry with backoff, download speed+ETA, clipboard monitor, toast notifications
# v0.6.1: Pexels profile hardened (Canva HD/UHD MP4 extraction, OG metadata
#          parsing, URL slug titles, Load More pagination, CDN domain filter),
#          BackgroundWorker for non-blocking exports, search debouncing,
#          auto-metadata from video filenames (resolution/fps/clip_id),
#          generic h1 skip patterns, video element DOM observer
# v0.6.0: Multi-site support with Site Profile system (Artlist, Pexels, Pixabay,
#          Storyblocks, Generic), expanded video detection (M3U8, MP4, WebM, DASH,
#          MOV), JSON-LD + OpenGraph metadata extraction, XHR/fetch/DOM video
#          interception, profile-driven URL classification and filtering
# v0.5.0: Cards-only view, always-visible detail panel, video auto-play on select,
#          hover video preview on cards, star ratings, favorites, notes/tags, collections,
#          saved searches, AND/OR mode, duration filter, card context menus
# v0.7.1: Audit fixes — route handler precedence bug, SQL column whitelist hardening,
#          thread-safe Row-to-dict conversion, FTS rebuild mechanism, bootstrap guard,
#          diagnostic logging for DB/DL errors, download worker stop race fix,
#          clipboard monitor opt-in, consolidated imports, unbounded fetchall limits

import sys, os, subprocess, traceback, re, random

# ─────────────────────────────────────────────────────────────────────────────
# CRASH HANDLER
# ─────────────────────────────────────────────────────────────────────────────

def _crash_handler(exc_type, exc_value, exc_tb):
    msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    crash_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'crash.log')
    try:
        with open(crash_file, 'w') as f: f.write(msg)
    except Exception: pass
    print(f"[FATAL]\n{msg}")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, f"Fatal error:\n{crash_file}\n\n{msg[:800]}", "Artlist Scraper — Fatal Error", 0x10)
    except Exception: pass
    sys.exit(1)

sys.excepthook = _crash_handler

# ─────────────────────────────────────────────────────────────────────────────
# AUTO-BOOTSTRAP
# ─────────────────────────────────────────────────────────────────────────────

def _install_chromium(verbose=True):
    """Install Playwright's Chromium browser. Returns True on success."""
    if verbose: print("[Setup] Installing Playwright Chromium browser...")
    # Try normal install first, then with --with-deps (Linux), then without
    for extra in [[], ['--with-deps']]:
        try:
            result = subprocess.run(
                [sys.executable, '-m', 'playwright', 'install', 'chromium'] + extra,
                capture_output=not verbose, timeout=300)
            if result.returncode == 0:
                if verbose: print("[Setup] Chromium installed successfully.")
                return True
        except Exception:
            continue
    return False


def _chromium_is_ready():
    """Check if Playwright's Chromium executable actually exists."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe = p.chromium.executable_path
            return os.path.isfile(exe)
    except Exception:
        return False


def _bootstrap():
    if sys.version_info < (3, 8):
        print("Python 3.8+ required"); sys.exit(1)

    try:
        import pip
    except ImportError:
        subprocess.check_call([sys.executable, '-m', 'ensurepip', '--default-pip'])

    # Install Python packages
    for pkg in ['PyQt6', 'playwright', 'imageio-ffmpeg']:
        import_name = pkg.replace('-', '_').lower()
        try:
            __import__(import_name)
        except ImportError:
            print(f"[Setup] Installing {pkg}...")
            installed = False
            for flags in [[], ['--user'], ['--break-system-packages']]:
                try:
                    subprocess.check_call(
                        [sys.executable, '-m', 'pip', 'install', pkg, '-q'] + flags,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    installed = True
                    break
                except subprocess.CalledProcessError: continue
            if not installed:
                print(f"[Setup] WARNING: could not install {pkg}")

    # Install Chromium browser if missing
    if not _chromium_is_ready():
        _install_chromium(verbose=True)
        # Don't abort if it fails — the GUI will show a clear error + install button

    print("[Setup] Ready.\n")

# Only run bootstrap when executed directly — not on library import
if __name__ == '__main__':
    _bootstrap()

# ─────────────────────────────────────────────────────────────────────────────
# IMPORTS
# ─────────────────────────────────────────────────────────────────────────────

import json, sqlite3, asyncio, threading
from datetime import datetime
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode, unquote

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QPushButton, QLineEdit, QTextEdit, QSpinBox,
    QCheckBox, QGroupBox, QFileDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QProgressBar, QFrame, QComboBox, QMenu,
    QScrollArea, QStatusBar, QMessageBox, QAbstractItemView,
    QSplitter, QSlider, QStackedWidget, QSizePolicy, QLayout,
    QSystemTrayIcon, QGraphicsOpacityEffect
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QSize, QRect, QPoint, QUrl,
    QPropertyAnimation, QEasingCurve
)
from PyQt6.QtGui import (
    QColor, QTextCursor, QPixmap, QPainter, QBrush, QFont,
    QKeySequence, QShortcut, QAction, QIcon
)

# Optional: in-app video preview (requires PyQt6-Multimedia)
_HAS_VIDEO = False
try:
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PyQt6.QtMultimediaWidgets import QVideoWidget
    _HAS_VIDEO = True
except ImportError:
    pass

# ─────────────────────────────────────────────────────────────────────────────
# DPI SCALING
# ─────────────────────────────────────────────────────────────────────────────

_dpi_factor = 1.0

def _init_dpi():
    """Compute DPI scale factor from primary screen. Call after QApplication init."""
    global _dpi_factor
    try:
        screen = QApplication.primaryScreen()
        if screen:
            ldpi = screen.logicalDotsPerInch()
            _dpi_factor = ldpi / 96.0
            if _dpi_factor < 1.0:
                _dpi_factor = 1.0
    except Exception:
        _dpi_factor = 1.0

def S(px):
    """Scale a pixel value by the DPI factor. Returns int.
    NOTE: Available as a utility for DPI-aware sizing. Not yet wired into
    widget construction — call S(px) instead of raw pixel values when
    hardcoded sizes need to respect high-DPI screens.
    """
    return max(1, int(px * _dpi_factor))


# ─────────────────────────────────────────────────────────────────────────────
# DARK THEME
# ─────────────────────────────────────────────────────────────────────────────

DARK_STYLE = """
/* ── Base ────────────────────────────────────────────────────────────── */
QMainWindow, QDialog, QWidget { background-color: #1e1e2e; color: #cdd6f4; font-family: 'Segoe UI', 'SF Pro', system-ui, sans-serif; }
QDialog QLabel { color: #cdd6f4; }
QDialog QLineEdit { background-color: #313244; color: #cdd6f4; border: 1px solid #45475a; border-radius: 5px; padding: 7px 10px; }
QDialog QPushButton { min-width: 80px; }

/* ── Tabs ────────────────────────────────────────────────────────────── */
QTabWidget::pane { border: 1px solid #313244; background: #1e1e2e; border-radius: 4px; }
QTabBar::tab { background: #181825; color: #6c7086; padding: 9px 20px; border-bottom: 2px solid transparent; margin-right: 2px; }
QTabBar::tab:selected { color: #cdd6f4; border-bottom: 2px solid #89b4fa; background: #1e1e2e; }
QTabBar::tab:hover:!selected { color: #a6adc8; background: #313244; }

/* ── Buttons ─────────────────────────────────────────────────────────── */
QPushButton { background-color: #89b4fa; color: #1e1e2e; border: none; padding: 8px 18px; border-radius: 6px; font-weight: 600; }
QPushButton:hover { background-color: #b4d0fb; }
QPushButton:pressed { background-color: #74c7ec; }
QPushButton:disabled { background-color: #313244; color: #585b70; }
QPushButton:checked { background-color: #74c7ec; color: #1e1e2e; border: 1px solid #89b4fa; }
QPushButton#danger  { background-color: #f38ba8; }
QPushButton#danger:hover  { background-color: #f5a0b8; }
QPushButton#success { background-color: #a6e3a1; color: #1e1e2e; }
QPushButton#success:hover { background-color: #b8f0b3; }
QPushButton#warning { background-color: #f9e2af; color: #1e1e2e; }
QPushButton#warning:hover { background-color: #faefc5; }
QPushButton#neutral { background-color: #313244; color: #cdd6f4; }
QPushButton#neutral:hover { background-color: #45475a; }
QPushButton#neutral:checked { background-color: #45475a; border: 1px solid #89b4fa; color: #89b4fa; }

/* ── Inputs ──────────────────────────────────────────────────────────── */
QLineEdit, QSpinBox, QComboBox, QDoubleSpinBox {
    background-color: #313244; color: #cdd6f4; border: 1px solid #45475a;
    border-radius: 5px; padding: 6px 10px;
    selection-background-color: #89b4fa; selection-color: #1e1e2e;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #89b4fa; }
QSpinBox::up-button, QSpinBox::down-button { background: #45475a; border: none; border-radius: 3px; width: 18px; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: #585b70; }
QSpinBox::up-arrow { image: none; border-left: 4px solid transparent; border-right: 4px solid transparent; border-bottom: 5px solid #cdd6f4; }
QSpinBox::down-arrow { image: none; border-left: 4px solid transparent; border-right: 4px solid transparent; border-top: 5px solid #cdd6f4; }
QComboBox::drop-down { border: none; width: 24px; subcontrol-position: right center; }
QComboBox::down-arrow { image: none; border-left: 4px solid transparent; border-right: 4px solid transparent; border-top: 5px solid #cdd6f4; }
QComboBox QAbstractItemView {
    background-color: #1e1e2e; color: #cdd6f4; border: 1px solid #45475a;
    selection-background-color: #313244; selection-color: #89b4fa;
    padding: 4px; outline: none;
}
QComboBox QAbstractItemView::item { padding: 4px 8px; border-radius: 3px; }
QComboBox QAbstractItemView::item:selected { background-color: #313244; }

/* ── Text areas ──────────────────────────────────────────────────────── */
QTextEdit, QPlainTextEdit {
    background-color: #11111b; color: #a6e3a1; border: 1px solid #313244;
    border-radius: 5px; padding: 8px;
    font-family: 'Cascadia Code', 'Consolas', monospace;
}

/* ── Groups / Checkboxes ─────────────────────────────────────────────── */
QGroupBox { border: 1px solid #313244; border-radius: 8px; margin-top: 14px; padding: 14px 12px 10px 12px; color: #cdd6f4; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #89b4fa; }
QCheckBox { color: #cdd6f4; spacing: 6px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 3px; border: 1px solid #45475a; background: #313244; }
QCheckBox::indicator:checked { background-color: #89b4fa; border-color: #89b4fa; }
QCheckBox::indicator:hover { border-color: #89b4fa; }

/* ── Labels ──────────────────────────────────────────────────────────── */
QLabel { color: #cdd6f4; background: transparent; }
QLabel#subtext { color: #6c7086; }

/* ── Progress bars ───────────────────────────────────────────────────── */
QProgressBar { background-color: #313244; border: none; border-radius: 5px; text-align: center; color: #cdd6f4; height: 8px; }
QProgressBar::chunk { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #89b4fa, stop:1 #74c7ec); border-radius: 5px; }

/* ── Tables ──────────────────────────────────────────────────────────── */
QTableWidget { background-color: #11111b; alternate-background-color: #181825; color: #cdd6f4; border: 1px solid #313244; gridline-color: #1e1e2e; }
QTableWidget::item { padding: 4px 8px; }
QTableWidget::item:selected { background-color: #313244; color: #89b4fa; }
QHeaderView::section { background-color: #181825; color: #6c7086; border: none; border-right: 1px solid #313244; border-bottom: 1px solid #313244; padding: 6px 8px; font-weight: 600; }

/* ── Scroll bars ─────────────────────────────────────────────────────── */
QScrollBar:vertical { background: #181825; width: 8px; border: none; }
QScrollBar::handle:vertical { background: #45475a; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #585b70; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #181825; height: 8px; border: none; }
QScrollBar::handle:horizontal { background: #45475a; border-radius: 4px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: #585b70; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

/* ── Status bar ──────────────────────────────────────────────────────── */
QStatusBar { background-color: #181825; color: #6c7086; border-top: 1px solid #313244; padding: 0 10px; }

/* ── Cards + stat panels ─────────────────────────────────────────────── */
QFrame#stat-card { background-color: #181825; border: 1px solid #313244; border-radius: 8px; }
QFrame#clip-card { background-color: #181825; border: 1px solid #313244; border-radius: 8px; }
QFrame#clip-card:hover { border-color: #89b4fa; background-color: #1e1e35; }
QPushButton#tag-chip {
    background: #313244; color: #cba6f7;
    font-size: 9px; padding: 1px 6px;
    border-radius: 3px; font-weight: 600;
    border: none; text-align: left;
}
QPushButton#tag-chip:hover { background: #45475a; }

/* ── Sliders ─────────────────────────────────────────────────────────── */
QSlider::groove:horizontal { background:#313244; height:4px; border-radius:2px; }
QSlider::handle:horizontal { background:#89b4fa; width:14px; height:14px; margin:-5px 0; border-radius:7px; }
QSlider::handle:horizontal:hover { background:#b4d0fb; }
QSlider::sub-page:horizontal { background:#89b4fa; border-radius:2px; }

/* ── Menus ────────────────────────────────────────────────────────────── */
QMenu { background:#181825; color:#cdd6f4; border:1px solid #313244; padding:4px; border-radius:6px; }
QMenu::item { padding:6px 24px 6px 16px; border-radius:4px; }
QMenu::item:selected { background:#313244; color:#89b4fa; }
QMenu::item:disabled { color:#585b70; }
QMenu::separator { height:1px; background:#313244; margin:4px 8px; }
QMenu::right-arrow { width:12px; height:12px; }

/* ── Tooltips ────────────────────────────────────────────────────────── */
QToolTip { background:#1e1e2e; color:#cdd6f4; border:1px solid #313244; padding:4px 8px; border-radius:4px; }

/* ── Scroll areas (transparent bg) ───────────────────────────────────── */
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }

/* ── Separator frames ────────────────────────────────────────────────── */
QFrame[frameShape="4"], QFrame[frameShape="5"] { color: #313244; }
"""

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE — expanded schema with full clip metadata + FTS search
# ─────────────────────────────────────────────────────────────────────────────

class DB:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init()

    @staticmethod
    def _rows_to_dicts(rows):
        """Convert sqlite3.Row objects to plain dicts for thread-safe passing."""
        if not rows:
            return rows
        return [dict(zip(r.keys(), tuple(r))) for r in rows]

    def _init(self):
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS clips (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                clip_id         TEXT UNIQUE,
                source_url      TEXT,
                title           TEXT,
                creator         TEXT,
                collection      TEXT,
                resolution      TEXT,
                duration        TEXT,
                frame_rate      TEXT,
                camera          TEXT,
                formats         TEXT,
                tags            TEXT,
                m3u8_url        TEXT DEFAULT '',
                thumbnail_url   TEXT DEFAULT '',
                found_at        DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS clips_fts USING fts5(
                title, creator, collection, tags, resolution, camera, duration,
                content='clips', content_rowid='id',
                tokenize='porter unicode61'
            );

            CREATE TABLE IF NOT EXISTS crawled_pages (
                url         TEXT PRIMARY KEY,
                status      TEXT DEFAULT 'pending',
                depth       INTEGER DEFAULT 0,
                profile     TEXT DEFAULT '',
                crawled_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS crawl_queue (
                url         TEXT PRIMARY KEY,
                depth       INTEGER DEFAULT 0,
                priority    INTEGER DEFAULT 0,
                profile     TEXT DEFAULT '',
                added_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_clips_creator    ON clips(creator);
            CREATE INDEX IF NOT EXISTS idx_clips_collection ON clips(collection);
            CREATE INDEX IF NOT EXISTS idx_queue_pri        ON crawl_queue(priority DESC, added_at ASC);
        """)
        # Add new columns if upgrading from older DB (safe to call repeatedly)
        for col, defn in [('local_path',  'TEXT DEFAULT ""'),
                          ('dl_status',   'TEXT DEFAULT ""'),
                          ('thumb_path',  'TEXT DEFAULT ""'),
                          ('user_rating', 'INTEGER DEFAULT 0'),
                          ('user_notes',  'TEXT DEFAULT ""'),
                          ('user_tags',   'TEXT DEFAULT ""'),
                          ('favorited',   'INTEGER DEFAULT 0'),
                          ('source_site', 'TEXT DEFAULT ""')]:
            try:
                self.conn.execute(f"ALTER TABLE clips ADD COLUMN {col} {defn}")
            except sqlite3.OperationalError:
                pass  # Column already exists
        # Migrate queue tables: add profile column if upgrading from older DB
        for tbl in ('crawl_queue', 'crawled_pages'):
            try:
                self.conn.execute(f'ALTER TABLE {tbl} ADD COLUMN profile TEXT DEFAULT ""')
            except sqlite3.OperationalError:
                pass  # Column already exists
        # Collections system
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS collections (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT UNIQUE NOT NULL,
                color       TEXT DEFAULT '#89b4fa',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS clip_collections (
                clip_id         TEXT NOT NULL,
                collection_id   INTEGER NOT NULL,
                added_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (clip_id, collection_id)
            );
            CREATE TABLE IF NOT EXISTS saved_searches (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT UNIQUE NOT NULL,
                query   TEXT DEFAULT '',
                filters TEXT DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.conn.commit()

    def execute(self, sql, params=()):
        with self._lock:
            return self.conn.execute(sql, params)

    def commit(self):
        with self._lock:
            self.conn.commit()

    # ── Queue ──────────────────────────────────────────────────────────────

    def enqueue(self, url, depth=0, priority=0, profile=''):
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT OR IGNORE INTO crawl_queue(url,depth,priority,profile) VALUES(?,?,?,?)",
                    (url, depth, priority, profile))
                self.conn.commit()
        except Exception as e:
            print(f"[DB WARN] enqueue failed for {url[:60]}: {e}")

    def dequeue(self, profile=None):
        with self._lock:
            if profile:
                row = self.conn.execute(
                    "SELECT url,depth FROM crawl_queue WHERE profile=? ORDER BY priority DESC, added_at ASC LIMIT 1",
                    (profile,)).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT url,depth FROM crawl_queue ORDER BY priority DESC, added_at ASC LIMIT 1").fetchone()
            if not row: return None
            self.conn.execute("DELETE FROM crawl_queue WHERE url=?", (row['url'],))
            self.conn.commit()
            return dict(row)

    def queue_size(self, profile=None):
        if profile:
            return self.execute("SELECT COUNT(*) FROM crawl_queue WHERE profile=?", (profile,)).fetchone()[0]
        return self.execute("SELECT COUNT(*) FROM crawl_queue").fetchone()[0]

    def is_processed(self, url):
        return bool(self.execute(
            "SELECT 1 FROM crawled_pages WHERE url=? AND status='done'", (url,)).fetchone())

    def mark_processed(self, url, depth=0):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO crawled_pages(url,status,depth,crawled_at) VALUES(?,?,?,?)",
                (url, 'done', depth, datetime.now().isoformat()))
            self.conn.commit()

    def mark_failed(self, url, depth=0):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO crawled_pages(url,status,depth,crawled_at) VALUES(?,?,?,?)",
                (url, 'failed', depth, datetime.now().isoformat()))
            self.conn.commit()

    # ── Clips ──────────────────────────────────────────────────────────────

    def save_clip(self, data: dict) -> bool:
        """Insert clip with full metadata. Returns True if new row."""
        try:
            with self._lock:
                cur = self.conn.execute("""
                    INSERT OR IGNORE INTO clips
                    (clip_id,source_url,title,creator,collection,resolution,
                     duration,frame_rate,camera,formats,tags,m3u8_url,thumbnail_url,source_site)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    str(data.get('clip_id','') or ''),
                    str(data.get('source_url','') or ''),
                    str(data.get('title','') or ''),
                    str(data.get('creator','') or ''),
                    str(data.get('collection','') or ''),
                    str(data.get('resolution','') or ''),
                    str(data.get('duration','') or ''),
                    str(data.get('frame_rate','') or ''),
                    str(data.get('camera','') or ''),
                    str(data.get('formats','') or ''),
                    str(data.get('tags','') or ''),
                    str(data.get('m3u8_url','') or ''),
                    str(data.get('thumbnail_url','') or ''),
                    str(data.get('source_site','') or ''),
                ))
                is_new = cur.rowcount > 0
                if is_new:
                    rowid = cur.lastrowid
                    self.conn.execute("""
                        INSERT INTO clips_fts(rowid,title,creator,collection,tags,resolution,camera,duration)
                        VALUES (?,?,?,?,?,?,?,?)
                    """, (rowid,
                          data.get('title',''), data.get('creator',''),
                          data.get('collection',''), data.get('tags',''),
                          data.get('resolution',''), data.get('camera',''),
                          data.get('duration','')))
                self.conn.commit()
                return is_new
        except Exception as e:
            print(f"[DB WARN] save_clip failed for {data.get('clip_id','?')}: {e}")
            return False

    def update_m3u8(self, clip_id, m3u8_url):
        """Upgrade video URL if new one is higher quality than existing."""
        try:
            with self._lock:
                row = self.conn.execute(
                    "SELECT m3u8_url, resolution FROM clips WHERE clip_id=?",
                    (clip_id,)).fetchone()
                if not row:
                    return 'not_found'
                existing = row['m3u8_url'] or ''
                if not existing:
                    # No existing URL — just set it
                    self.conn.execute(
                        "UPDATE clips SET m3u8_url=? WHERE clip_id=?",
                        (m3u8_url, clip_id))
                    self.conn.commit()
                    return 'set_new'
                if existing == m3u8_url:
                    return 'same'
                # Compare quality scores
                new_score = self._url_quality_score(m3u8_url)
                old_score = self._url_quality_score(existing)
                if new_score > old_score:
                    self.conn.execute(
                        "UPDATE clips SET m3u8_url=? WHERE clip_id=?",
                        (m3u8_url, clip_id))
                    # Also upgrade resolution/formats from the new URL
                    res_m = re.search(r'(\d{3,4})_(\d{3,4})_(\d+)fps', m3u8_url)
                    if res_m:
                        w, h = res_m.group(1), res_m.group(2)
                        self.conn.execute(
                            "UPDATE clips SET resolution=?, frame_rate=? WHERE clip_id=?",
                            (f"{w}x{h}", res_m.group(3), clip_id))
                    qual_m = re.search(r'-(uhd|hd|sd)_', m3u8_url, re.IGNORECASE)
                    if qual_m:
                        self.conn.execute(
                            "UPDATE clips SET formats=? WHERE clip_id=?",
                            (qual_m.group(1).upper(), clip_id))
                    self.conn.commit()
                    return 'upgraded'
                return 'kept_existing'
        except Exception as e:
            print(f"[DB WARN] update_m3u8 failed for {clip_id}: {e}")
            return 'error'

    @staticmethod
    def _url_quality_score(url):
        """Score a video URL by quality. Higher = better."""
        if not url:
            return 0
        # Parse resolution from filename pattern: WxH_FPSfps or W_H_FPSfps
        m = re.search(r'(\d{3,4})_(\d{3,4})_(\d+)fps', url)
        if m:
            return max(int(m.group(1)), int(m.group(2)))
        # Fallback: quality tag
        if '-uhd_' in url or 'uhd' in url.lower(): return 2560
        if '-hd_' in url: return 1080
        if '-sd_' in url: return 360
        # M3U8 streams are generally adaptive (high quality)
        if '.m3u8' in url: return 2000
        return 100

    def update_metadata(self, clip_id, data: dict):
        """Fill in metadata fields on an existing record.
        Most fields: only fill if currently empty.
        resolution/formats/m3u8_url: upgrade if new value is higher quality.
        """
        _ALLOWED_META_FIELDS = frozenset({
            'title','creator','collection','resolution','duration',
            'frame_rate','camera','formats','tags','thumbnail_url','m3u8_url'
        })
        fields = ['title','creator','collection','resolution','duration',
                  'frame_rate','camera','formats','tags','thumbnail_url','m3u8_url']
        # Fields that can be upgraded (not just fill-empty)
        upgrade_fields = {'resolution', 'formats', 'frame_rate'}
        sets, vals = [], []
        for f in fields:
            # Belt-and-suspenders: validate field name against whitelist
            if f not in _ALLOWED_META_FIELDS:
                continue
            v = str(data.get(f, '') or '').strip()
            if not v:
                continue
            if f == 'm3u8_url':
                continue  # Handled by update_m3u8 with quality comparison
            if f in upgrade_fields:
                # Allow upgrade: overwrite if new value represents higher quality
                sets.append(f"{f} = ?")
                vals.append(v)
            else:
                # Fill only if empty
                sets.append(f"{f} = CASE WHEN ({f} IS NULL OR {f}='') THEN ? ELSE {f} END")
                vals.append(v)
        if not sets: return
        vals.append(clip_id)
        try:
            with self._lock:
                self.conn.execute(f"UPDATE clips SET {', '.join(sets)} WHERE clip_id=?", vals)
                # Also re-index FTS
                row = self.conn.execute(
                    "SELECT id,title,creator,collection,tags,resolution,camera,duration FROM clips WHERE clip_id=?",
                    (clip_id,)).fetchone()
                if row:
                    self.conn.execute("DELETE FROM clips_fts WHERE rowid=?", (row['id'],))
                    self.conn.execute("""
                        INSERT INTO clips_fts(rowid,title,creator,collection,tags,resolution,camera,duration)
                        VALUES (?,?,?,?,?,?,?,?)
                    """, (row['id'], row['title'] or '', row['creator'] or '',
                          row['collection'] or '', row['tags'] or '',
                          row['resolution'] or '', row['camera'] or '', row['duration'] or ''))
                self.conn.commit()
        except Exception as e:
            print(f"[DB WARN] update_metadata failed for {clip_id}: {e}")

    def search(self, query='', filters=None, limit=3000, offset=0):
        """
        filters: dict of {column: value} — all are ANDed.
        Legacy positional args (filter_col, filter_val) accepted for compatibility.
        """
        if filters is None: filters = {}
        if query and query.strip():
            raw = query.strip()
            words = raw.split()
            terms = ' OR '.join(f'"{w}"' if len(w) > 1 else w for w in words)
            sql = """
                SELECT c.* FROM clips c
                JOIN clips_fts f ON c.id = f.rowid
                WHERE clips_fts MATCH ?
            """
            params = [terms]
            for col, val in filters.items():
                if col not in self._VALID_COLUMNS:
                    print(f"[DB WARN] Rejected invalid filter column: {col!r}")
                    continue
                if val and val != 'All':
                    sql += f" AND c.{col} = ?"
                    params.append(val)
            sql += " ORDER BY rank LIMIT ? OFFSET ?"
            params += [limit, offset]
        else:
            sql = "SELECT * FROM clips WHERE 1=1"
            params = []
            for col, val in filters.items():
                if col not in self._VALID_COLUMNS:
                    print(f"[DB WARN] Rejected invalid filter column: {col!r}")
                    continue
                if val and val != 'All':
                    sql += f" AND {col} = ?"
                    params.append(val)
            sql += " ORDER BY found_at DESC LIMIT ? OFFSET ?"
            params += [limit, offset]
        try:
            with self._lock:
                return self.conn.execute(sql, params).fetchall()
        except Exception:
            with self._lock:
                return self.conn.execute(
                    "SELECT * FROM clips ORDER BY found_at DESC LIMIT ? OFFSET ?",
                    (limit, offset)).fetchall()

    def update_thumb_path(self, clip_id, thumb_path):
        try:
            with self._lock:
                self.conn.execute("UPDATE clips SET thumb_path=? WHERE clip_id=?", (thumb_path, clip_id))
                self.conn.commit()
        except Exception as e:
            print(f"[DB WARN] update_thumb_path failed for {clip_id}: {e}")

    def get_clips_needing_thumbs(self, limit=300):
        """Clips with M3U8/local_path but no thumbnail yet."""
        return self.execute("""
            SELECT * FROM clips
            WHERE (thumb_path IS NULL OR thumb_path = '')
              AND (m3u8_url != '' OR local_path != '' OR thumbnail_url != '')
            ORDER BY found_at DESC LIMIT ?
        """, (limit,)).fetchall()

    _VALID_COLUMNS = frozenset({
        'creator','collection','resolution','frame_rate','dl_status',
        'title','tags','camera','duration','formats','clip_id',
        'user_rating','user_tags','favorited',
    })

    def distinct_values(self, col):
        if col not in self._VALID_COLUMNS: return []
        rows = self.execute(
            f"SELECT DISTINCT {col} FROM clips WHERE {col} IS NOT NULL AND {col} != '' ORDER BY {col}"
        ).fetchall()
        return [r[0] for r in rows]

    def clip_count(self):  return self.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    def m3u8_count(self):  return self.execute("SELECT COUNT(*) FROM clips WHERE m3u8_url != ''").fetchone()[0]
    def proc_count(self):  return self.execute("SELECT COUNT(*) FROM crawled_pages WHERE status='done'").fetchone()[0]
    def fail_count(self):  return self.execute("SELECT COUNT(*) FROM crawled_pages WHERE status='failed'").fetchone()[0]

    def stats(self):
        return {'clips': self.clip_count(), 'm3u8': self.m3u8_count(),
                'processed': self.proc_count(), 'failed': self.fail_count(),
                'queued': self.queue_size()}

    def all_clips(self, limit=50000):
        return self.execute("SELECT * FROM clips ORDER BY found_at ASC LIMIT ?", (limit,)).fetchall()

    def clips_with_m3u8(self, only_undownloaded=False, limit=50000):
        """Return clips that have an M3U8 URL, optionally filtering to not-yet-downloaded."""
        if only_undownloaded:
            return self.execute(
                "SELECT * FROM clips WHERE m3u8_url != '' AND (local_path IS NULL OR local_path = '') ORDER BY found_at ASC LIMIT ?",
                (limit,)).fetchall()
        return self.execute(
            "SELECT * FROM clips WHERE m3u8_url != '' ORDER BY found_at ASC LIMIT ?",
            (limit,)).fetchall()

    def update_local_path(self, clip_id, local_path, dl_status='done'):
        """Record the downloaded file path and status."""
        try:
            with self._lock:
                self.conn.execute(
                    "UPDATE clips SET local_path=?, dl_status=? WHERE clip_id=?",
                    (local_path, dl_status, clip_id))
                self.conn.commit()
        except Exception as e:
            print(f"[DB WARN] update_local_path failed for {clip_id}: {e}")

    def set_dl_status(self, clip_id, status):
        try:
            with self._lock:
                self.conn.execute("UPDATE clips SET dl_status=? WHERE clip_id=?", (status, clip_id))
                self.conn.commit()
        except Exception as e:
            print(f"[DB WARN] set_dl_status failed for {clip_id}: {e}")

    # ── Asset Management ─────────────────────────────────────────────────────

    def set_rating(self, clip_id, rating):
        try:
            with self._lock:
                self.conn.execute("UPDATE clips SET user_rating=? WHERE clip_id=?",
                                  (max(0, min(5, int(rating))), clip_id))
                self.conn.commit()
        except Exception as e:
            print(f"[DB WARN] set_rating failed for {clip_id}: {e}")

    def set_notes(self, clip_id, notes):
        try:
            with self._lock:
                self.conn.execute("UPDATE clips SET user_notes=? WHERE clip_id=?", (str(notes), clip_id))
                self.conn.commit()
        except Exception as e:
            print(f"[DB WARN] set_notes failed for {clip_id}: {e}")

    def set_user_tags(self, clip_id, tags):
        """Set user-defined tags (comma-separated string). Also re-indexes FTS."""
        try:
            with self._lock:
                self.conn.execute("UPDATE clips SET user_tags=? WHERE clip_id=?", (str(tags), clip_id))
                # Re-index FTS to include user_tags in search
                row = self.conn.execute(
                    "SELECT id,title,creator,collection,tags,resolution,camera,duration,user_tags FROM clips WHERE clip_id=?",
                    (clip_id,)).fetchone()
                if row:
                    all_tags = ', '.join(filter(None, [row['tags'] or '', row['user_tags'] or '']))
                    self.conn.execute("DELETE FROM clips_fts WHERE rowid=?", (row['id'],))
                    self.conn.execute("""
                        INSERT INTO clips_fts(rowid,title,creator,collection,tags,resolution,camera,duration)
                        VALUES (?,?,?,?,?,?,?,?)
                    """, (row['id'], row['title'] or '', row['creator'] or '',
                          row['collection'] or '', all_tags,
                          row['resolution'] or '', row['camera'] or '', row['duration'] or ''))
                self.conn.commit()
        except Exception as e:
            print(f"[DB WARN] set_user_tags failed for {clip_id}: {e}")

    def toggle_favorite(self, clip_id):
        """Toggle favorited state. Returns new state (0 or 1)."""
        try:
            with self._lock:
                row = self.conn.execute("SELECT favorited FROM clips WHERE clip_id=?", (clip_id,)).fetchone()
                new_state = 0 if (row and row['favorited']) else 1
                self.conn.execute("UPDATE clips SET favorited=? WHERE clip_id=?", (new_state, clip_id))
                self.conn.commit()
                return new_state
        except Exception:
            return 0

    # ── Collections ────────────────────────────────────────────────────────

    def create_collection(self, name, color='#89b4fa'):
        try:
            with self._lock:
                self.conn.execute("INSERT OR IGNORE INTO collections(name,color) VALUES(?,?)", (name, color))
                self.conn.commit()
                return self.conn.execute("SELECT id FROM collections WHERE name=?", (name,)).fetchone()['id']
        except Exception:
            return None

    def delete_collection(self, collection_id):
        try:
            with self._lock:
                self.conn.execute("DELETE FROM clip_collections WHERE collection_id=?", (collection_id,))
                self.conn.execute("DELETE FROM collections WHERE id=?", (collection_id,))
                self.conn.commit()
        except Exception as e:
            print(f"[DB WARN] delete_collection failed for {collection_id}: {e}")

    def get_collections(self):
        return self.execute("SELECT * FROM collections ORDER BY name").fetchall()

    def add_to_collection(self, clip_id, collection_id):
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT OR IGNORE INTO clip_collections(clip_id,collection_id) VALUES(?,?)",
                    (clip_id, collection_id))
                self.conn.commit()
        except Exception as e:
            print(f"[DB WARN] add_to_collection failed: {e}")

    def remove_from_collection(self, clip_id, collection_id):
        try:
            with self._lock:
                self.conn.execute(
                    "DELETE FROM clip_collections WHERE clip_id=? AND collection_id=?",
                    (clip_id, collection_id))
                self.conn.commit()
        except Exception as e:
            print(f"[DB WARN] remove_from_collection failed: {e}")

    def get_clip_collections(self, clip_id):
        """Get all collections a clip belongs to."""
        return self.execute("""
            SELECT c.* FROM collections c
            JOIN clip_collections cc ON c.id = cc.collection_id
            WHERE cc.clip_id=? ORDER BY c.name
        """, (clip_id,)).fetchall()

    def collection_clip_count(self, collection_id):
        return self.execute(
            "SELECT COUNT(*) FROM clip_collections WHERE collection_id=?",
            (collection_id,)).fetchone()[0]

    # ── Saved Searches ─────────────────────────────────────────────────────

    def save_search(self, name, query, filters_json):
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT OR REPLACE INTO saved_searches(name,query,filters) VALUES(?,?,?)",
                    (name, query, filters_json))
                self.conn.commit()
        except Exception as e:
            print(f"[DB WARN] save_search failed: {e}")

    def get_saved_searches(self):
        return self.execute("SELECT * FROM saved_searches ORDER BY name").fetchall()

    def delete_saved_search(self, search_id):
        try:
            with self._lock:
                self.conn.execute("DELETE FROM saved_searches WHERE id=?", (search_id,))
                self.conn.commit()
        except Exception as e:
            print(f"[DB WARN] delete_saved_search failed: {e}")

    # ── Enhanced Search ────────────────────────────────────────────────────

    def search_assets(self, query='', filters=None, mode='OR',
                      favorites_only=False, downloaded_only=False,
                      duration_range=None, collection_id=None,
                      min_rating=0, limit=3000, offset=0):
        """
        Asset-oriented search with AND/OR mode, favorites, downloaded,
        duration range, collection filter, and rating filter.
        """
        if filters is None: filters = {}
        params = []

        # Base query with optional collection join
        if collection_id:
            base = """SELECT c.* FROM clips c
                      JOIN clip_collections cc ON c.clip_id = cc.clip_id
                      WHERE cc.collection_id = ?"""
            params.append(collection_id)
        elif query and query.strip():
            raw = query.strip()
            words = raw.split()
            if mode == 'AND':
                terms = ' AND '.join(f'"{w}"' for w in words)
            else:
                terms = ' OR '.join(f'"{w}"' if len(w) > 1 else w for w in words)
            base = """SELECT c.* FROM clips c
                      JOIN clips_fts f ON c.id = f.rowid
                      WHERE clips_fts MATCH ?"""
            params.append(terms)
        else:
            base = "SELECT c.* FROM clips c WHERE 1=1"

        # Column filters
        for col, val in filters.items():
            if col not in self._VALID_COLUMNS:
                print(f"[DB WARN] Rejected invalid filter column: {col!r}")
                continue
            if val and val != 'All':
                base += f" AND c.{col} = ?"
                params.append(val)

        # Favorites
        if favorites_only:
            base += " AND c.favorited = 1"

        # Downloaded only
        if downloaded_only:
            base += " AND c.dl_status = 'done' AND c.local_path != ''"

        # Rating filter
        if min_rating > 0:
            base += " AND c.user_rating >= ?"
            params.append(min_rating)

        # Duration range filter (duration is stored as 'MM:SS' or 'HH:MM:SS')
        if duration_range and duration_range != 'All':
            ranges = {
                '0-10s':   (0, 10),
                '10-30s':  (10, 30),
                '30s-1m':  (30, 60),
                '1-5m':    (60, 300),
                '5m+':     (300, 999999),
            }
            if duration_range in ranges:
                lo, hi = ranges[duration_range]
                # Parse MM:SS → seconds. For HH:MM:SS, treat first part as hours.
                # Uses: LENGTH - INSTR trick to find last colon position
                base += """ AND (
                    CASE
                        WHEN LENGTH(c.duration) - LENGTH(REPLACE(c.duration, ':', '')) >= 2 THEN
                            CAST(SUBSTR(c.duration, 1, INSTR(c.duration,':')-1) AS REAL)*3600 +
                            CAST(SUBSTR(SUBSTR(c.duration, INSTR(c.duration,':')+1), 1,
                                 INSTR(SUBSTR(c.duration, INSTR(c.duration,':')+1),':')-1) AS REAL)*60 +
                            CAST(SUBSTR(SUBSTR(c.duration, INSTR(c.duration,':')+1),
                                 INSTR(SUBSTR(c.duration, INSTR(c.duration,':')+1),':')+1) AS REAL)
                        WHEN c.duration LIKE '%:%' THEN
                            CAST(SUBSTR(c.duration, 1, INSTR(c.duration,':')-1) AS REAL)*60 +
                            CAST(SUBSTR(c.duration, INSTR(c.duration,':')+1) AS REAL)
                        ELSE 0
                    END
                ) BETWEEN ? AND ?"""
                params += [lo, hi]

        # Sort order
        if query and query.strip() and not collection_id:
            base += " ORDER BY rank"
        else:
            base += " ORDER BY c.found_at DESC"
        base += " LIMIT ? OFFSET ?"
        params += [limit, offset]

        try:
            with self._lock:
                return self.conn.execute(base, params).fetchall()
        except Exception:
            # Fallback
            with self._lock:
                return self.conn.execute(
                    "SELECT * FROM clips ORDER BY found_at DESC LIMIT ? OFFSET ?",
                    (limit, offset)).fetchall()

    def clear_all(self):
        with self._lock:
            self.conn.executescript("""
                DELETE FROM clips; DELETE FROM clips_fts;
                DELETE FROM crawled_pages; DELETE FROM crawl_queue;
                DELETE FROM clip_collections; DELETE FROM collections;
                DELETE FROM saved_searches;
            """)
            self.conn.commit()

    def rebuild_fts(self):
        """Rebuild the FTS5 index from scratch. Call if search seems out of sync."""
        try:
            with self._lock:
                self.conn.execute("DELETE FROM clips_fts")
                self.conn.execute("""
                    INSERT INTO clips_fts(rowid, title, creator, collection, tags,
                                          resolution, camera, duration)
                    SELECT id, COALESCE(title,''), COALESCE(creator,''),
                           COALESCE(collection,''),
                           COALESCE(tags,'') || ' ' || COALESCE(user_tags,''),
                           COALESCE(resolution,''), COALESCE(camera,''),
                           COALESCE(duration,'')
                    FROM clips
                """)
                self.conn.commit()
                count = self.conn.execute("SELECT COUNT(*) FROM clips_fts").fetchone()[0]
                print(f"[DB] FTS index rebuilt: {count} rows indexed")
                return count
        except Exception as e:
            print(f"[DB ERROR] FTS rebuild failed: {e}")
            return -1

    def close(self): self.conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

def get_config_dir():
    base = os.environ.get('APPDATA', os.path.expanduser('~'))
    p = os.path.join(base, 'ArtlistScraper')
    os.makedirs(p, exist_ok=True)
    return p

def load_config():
    f = os.path.join(get_config_dir(), 'config.json')
    try:
        with open(f) as fh: return json.load(fh)
    except Exception: return {}

def save_config(cfg):
    f = os.path.join(get_config_dir(), 'config.json')
    with open(f, 'w') as fh: json.dump(cfg, fh, indent=2)

# ─────────────────────────────────────────────────────────────────────────────
# SITE PROFILE SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

# Video URL patterns — compiled once, used by all profiles
VIDEO_PATTERNS = {
    'm3u8': re.compile(r'https?://[^\s"\'<>]+\.m3u8(?:\?[^\s"\'<>]*)?', re.IGNORECASE),
    'mp4':  re.compile(r'https?://[^\s"\'<>]+\.mp4(?:\?[^\s"\'<>]*)?', re.IGNORECASE),
    'webm': re.compile(r'https?://[^\s"\'<>]+\.webm(?:\?[^\s"\'<>]*)?', re.IGNORECASE),
    'mpd':  re.compile(r'https?://[^\s"\'<>]+\.mpd(?:\?[^\s"\'<>]*)?', re.IGNORECASE),
    'm3u':  re.compile(r'https?://[^\s"\'<>]+\.m3u(?:\?[^\s"\'<>]*)?', re.IGNORECASE),
    'mov':  re.compile(r'https?://[^\s"\'<>]+\.mov(?:\?[^\s"\'<>]*)?', re.IGNORECASE),
}
# Combined regex for "find any video URL in text"
ALL_VIDEO_RE = re.compile(
    r'https?://[^\s"\'<>]+\.(?:m3u8|mp4|webm|mpd|m3u|mov)(?:\?[^\s"\'<>]*)?', re.IGNORECASE)

# Common exclude patterns shared by most profiles
_COMMON_EXCLUDES = [
    '/login', '/register', '/signup', '/pricing', '/account', '/blog',
    '/about', '/careers', '/contact', '/legal', '/privacy',
    '/terms', '/help', '/support', '/faq', '/press',
    'javascript:', 'mailto:', 'tel:', '#',
]

class SiteProfile:
    """
    Defines how the crawler behaves on a specific site (or generically).

    Attributes:
        name            Display name for UI
        description     Short description
        domains         List of allowed domains (empty = allow all)
        start_url       Default start URL
        catalog_patterns  URL path substrings that identify listing/category pages
        item_patterns     URL path substrings that identify individual item pages
        exclude_patterns  URL path substrings to skip
        item_url_regex    Regex to identify item URLs (applied to full URL)
        video_types       Which VIDEO_PATTERNS keys to use (e.g. ['m3u8'] or ['mp4','webm'])
        scroll_items      Whether to scroll down item pages for related content
        metadata_selectors  Dict of field -> CSS selector for metadata extraction
        og_fallback       Use OpenGraph meta tags as metadata fallback
        jsonld_fallback   Try JSON-LD structured data
        custom_js         Extra JS to inject on pages (e.g. to trigger players)
    """

    # Built-in profile registry
    _registry = {}

    def __init__(self, name, **kw):
        self.name               = name
        self.description        = kw.get('description', '')
        self.domains            = kw.get('domains', [])
        self.start_url          = kw.get('start_url', '')
        self.catalog_patterns   = kw.get('catalog_patterns', [])
        self.item_patterns      = kw.get('item_patterns', [])
        self.exclude_patterns   = list(_COMMON_EXCLUDES) + kw.get('exclude_patterns', [])
        self.item_url_regex     = kw.get('item_url_regex', '')
        self.video_types        = kw.get('video_types', ['m3u8', 'mp4', 'webm', 'mpd'])
        self.scroll_items       = kw.get('scroll_items', True)
        self.metadata_selectors = kw.get('metadata_selectors', {})
        self.og_fallback        = kw.get('og_fallback', True)
        self.jsonld_fallback    = kw.get('jsonld_fallback', True)
        self.custom_js          = kw.get('custom_js', '')
        # Catalog pagination: CSS selector for "Load More" button, max clicks
        self.load_more_selector = kw.get('load_more_selector', '')
        self.load_more_clicks   = kw.get('load_more_clicks', 0)
        # Video URL domain filter (e.g. 'videos.pexels.com') — only record URLs from this domain
        self.video_cdn_domain   = kw.get('video_cdn_domain', '')

    def is_allowed_domain(self, domain):
        if not self.domains:
            return True
        return any(d in domain for d in self.domains)

    def is_catalog(self, url):
        if not self.catalog_patterns:
            return False
        return any(p in url for p in self.catalog_patterns)

    def is_item(self, url):
        """Check if URL is an individual item (clip/video/photo) page."""
        path = urlparse(url).path.rstrip('/')
        # If we have a regex, use it
        if self.item_url_regex:
            return bool(re.search(self.item_url_regex, url))
        # Otherwise check patterns + numeric final segment as hint
        if self.item_patterns:
            if not any(p in path for p in self.item_patterns):
                return False
            # If final segment is numeric, likely an item page
            final = path.split('/')[-1] if '/' in path else ''
            if final.isdigit():
                return True
        return False

    def is_excluded(self, url):
        return any(p in url for p in self.exclude_patterns)

    def normalize_url(self, url):
        try:
            u = urlparse(url)
            if not self.is_allowed_domain(u.netloc):
                return None
            skip = {'utm_source','utm_medium','utm_campaign','ref','fbclid','gclid','gad_source'}
            params = {k:v for k,v in parse_qs(u.query).items() if k not in skip}
            return urlunparse(u._replace(fragment='', query=urlencode(params, doseq=True)))
        except Exception:
            return None

    def get_video_regexes(self):
        """Return compiled regex patterns for the video types this profile cares about."""
        return [VIDEO_PATTERNS[t] for t in self.video_types if t in VIDEO_PATTERNS]

    def get_combined_video_re(self):
        """Single regex matching any of this profile's video types."""
        exts = '|'.join(self.video_types)
        return re.compile(
            rf'https?://[^\s"\'<>]+\.(?:{exts})(?:\?[^\s"\'<>]*)?', re.IGNORECASE)

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}

    @classmethod
    def from_dict(cls, d):
        d = dict(d)  # Shallow copy to avoid mutating caller's dict
        name = d.pop('name', 'Custom')
        # Remove _COMMON_EXCLUDES from stored excludes to avoid doubling
        stored_exc = d.get('exclude_patterns', [])
        d['exclude_patterns'] = [p for p in stored_exc if p not in _COMMON_EXCLUDES]
        return cls(name, **d)

    @classmethod
    def register(cls, profile):
        cls._registry[profile.name] = profile
        return profile

    @classmethod
    def get(cls, name):
        return cls._registry.get(name)

    @classmethod
    def all_names(cls):
        return list(cls._registry.keys())


# ── Built-in profiles ──────────────────────────────────────────────────────

SiteProfile.register(SiteProfile(
    'Artlist',
    description='Artlist.io stock footage — M3U8 HLS streams',
    domains=['artlist.io'],
    start_url='https://artlist.io/stock-footage/',
    catalog_patterns=['/stock-footage'],
    item_patterns=['/stock-footage/'],
    exclude_patterns=[
        '/sfx', '/stock-music', '/video-templates', '/song/',
        '/sound-effects', '/templates', '/playlist', '/browse',
        '/editorial', '/enterprise', '/teams', '/voice-over',
        '/royalty-free-music', '/luts', '/tools', '/favorites',
        '/downloads', '/spotlight', '/page/pricing',
    ],
    item_url_regex=r'/stock-footage/.+/\d{4,}$',
    video_types=['m3u8'],
    scroll_items=True,
    metadata_selectors={
        'clip_id':    r'Clip\s+ID\s+(\d+)',
        'resolution': r'Resolution\s+([\d]{3,4}\s*[xX\u00d7]\s*[\d]{3,4})',
        'duration':   r'Length\s+([\d:]{4,8})',
        'frame_rate': r'Frame\s+Rate\s+(\d+)',
        'camera':     r'Camera\s+([^\n\r]{2,50}?)(?:\n|\r|Available)',
        'formats':    r'Available\s+Formats\s+((?:(?:HD|SD|4K|2K|ProRes|MP4|MOV|RAW)\s*)+)',
        'creator':    r'Clip by\s*\n?\s*([^\n\r]{2,50})',
        'collection': r'Part of\s*\n?\s*([^\n\r]{2,60})',
        'tags':       r'Tags\s*\n((?:.+\n?){1,25}?)(?:Related|Part of|Clip by|Similar|Explore|$)',
    },
    og_fallback=True,
))

SiteProfile.register(SiteProfile(
    'Pexels',
    description='Pexels.com free stock videos — direct MP4 downloads (SD/HD/UHD)',
    domains=['pexels.com', 'www.pexels.com'],
    start_url='https://www.pexels.com/videos/',
    catalog_patterns=['/videos/', '/search/videos/', '/collections/'],
    item_patterns=['/video/'],
    exclude_patterns=[
        '/download/', '/license/', '/photo/', '/ja-jp/', '/ko-kr/',
        '/de-de/', '/fr-fr/', '/es-es/', '/pt-br/', '/zh-cn/', '/zh-tw/',
        '/ru-ru/', '/it-it/', '/nl-nl/', '/pl-pl/', '/sv-se/', '/tr-tr/',
        '/da-dk/', '/fi-fi/', '/nb-no/', '/cs-cz/', '/hu-hu/', '/ro-ro/',
        '/sk-sk/', '/uk-ua/', '/vi-vn/', '/th-th/', '/el-gr/', '/et-ee/',
        '/id-id/', '/ca-es/',
    ],
    item_url_regex=r'pexels\.com/video/[^/]+-\d+/?$',
    video_types=['mp4', 'webm'],
    video_cdn_domain='videos.pexels.com',
    scroll_items=True,
    metadata_selectors={},
    og_fallback=True,
    jsonld_fallback=True,
    load_more_selector='[class*="loadMore"], [class*="LoadMore"]',
    load_more_clicks=15,
))

SiteProfile.register(SiteProfile(
    'Pixabay',
    description='Pixabay.com free stock videos',
    domains=['pixabay.com', 'www.pixabay.com'],
    start_url='https://pixabay.com/videos/',
    catalog_patterns=['/videos/'],
    item_patterns=['/videos/'],
    item_url_regex=r'/videos/[^/]+-\d+/?$',
    video_types=['mp4', 'webm'],
    scroll_items=True,
    og_fallback=True,
    jsonld_fallback=True,
))

SiteProfile.register(SiteProfile(
    'Storyblocks',
    description='Storyblocks.com stock video — HLS streams',
    domains=['storyblocks.com', 'www.storyblocks.com'],
    start_url='https://www.storyblocks.com/video/',
    catalog_patterns=['/video/'],
    item_patterns=['/video/stock/'],
    item_url_regex=r'/video/stock/.+',
    video_types=['m3u8', 'mp4', 'webm'],
    scroll_items=True,
    og_fallback=True,
    jsonld_fallback=True,
))

SiteProfile.register(SiteProfile(
    'Generic',
    description='Auto-detect video streams on any site (M3U8, MP4, WebM, DASH)',
    domains=[],  # allow all
    start_url='',
    catalog_patterns=[],
    item_patterns=[],
    item_url_regex='',
    video_types=['m3u8', 'mp4', 'webm', 'mpd', 'mov'],
    scroll_items=True,
    og_fallback=True,
    jsonld_fallback=True,
))

# Convenience reference — kept for backward compat with existing code paths
M3U8_RE = VIDEO_PATTERNS['m3u8']


class BrowserInstallWorker(QThread):
    """Runs `playwright install chromium` in a background thread with live log output."""
    log_signal    = pyqtSignal(str)
    finished      = pyqtSignal(bool)   # True = success

    def run(self):
        import subprocess as _sp
        self.log_signal.emit("[Setup] Installing Playwright Chromium browser...")
        try:
            proc = _sp.Popen(
                [sys.executable, '-m', 'playwright', 'install', 'chromium'],
                stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True, bufsize=1)
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.log_signal.emit(f"[Browser] {line}")
            proc.wait()
            ok = proc.returncode == 0
            self.log_signal.emit(
                "[Setup] Chromium installed successfully." if ok
                else f"[Setup] Install failed (exit {proc.returncode}).")
            self.finished.emit(ok)
        except Exception as e:
            self.log_signal.emit(f"[Setup] Install error: {e}")
            self.finished.emit(False)


class BackgroundWorker(QThread):
    """
    Generic background worker — runs any callable off the GUI thread.
    Emits result_signal(object) on completion, error_signal(str) on failure.
    Usage:
        w = BackgroundWorker(func, arg1, arg2, kw1=val1)
        w.result_signal.connect(on_done)
        w.start()
    """
    result_signal = pyqtSignal(object)
    error_signal  = pyqtSignal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
            self.result_signal.emit(result)
        except Exception as e:
            self.error_signal.emit(f"{type(e).__name__}: {e}")


class CrawlerWorker(QThread):
    log_signal    = pyqtSignal(str, str)
    stats_signal  = pyqtSignal(dict)
    clip_signal   = pyqtSignal(dict)
    status_signal = pyqtSignal(str)
    finished      = pyqtSignal()

    def __init__(self, cfg, db, profile=None, profiles=None):
        super().__init__()
        self.cfg     = cfg
        self.db      = db
        if profiles and len(profiles) > 0:
            self._profiles = profiles
        elif profile:
            self._profiles = [profile]
        else:
            self._profiles = [SiteProfile.get('Artlist') or SiteProfile.get('Generic')]
        self.profile = self._profiles[0]
        self._stop   = threading.Event()
        self._pause  = threading.Event()
        self._video_re = self.profile.get_combined_video_re()
        self._batch_size = cfg.get('batch_size', 50)

    def stop(self):   self._stop.set()
    def pause(self):  self._pause.set()
    def resume(self): self._pause.clear()

    def log(self, msg, level='INFO'):
        self.log_signal.emit(f"[{datetime.now().strftime('%H:%M:%S')}] [{level:5s}] {msg}", level)

    def run(self):
        # Must create a NEW event loop per thread.
        # asyncio.run() fails in QThread on Python 3.12+ because Qt owns the
        # main thread loop.  new_event_loop() avoids the conflict entirely.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._crawl())
        except Exception as e:
            import traceback as _tb
            msg = f"Crawler crashed: {type(e).__name__}: {e}\n\n{_tb.format_exc()}"
            self.log_signal.emit(f"[FATAL] {msg}", "ERROR")
            self.status_signal.emit("stopped")
            self.finished.emit()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except (RuntimeError, asyncio.CancelledError):
                pass  # Expected during event loop teardown
            loop.close()

    # ── Stealth patches ─────────────────────────────────────────────────────

    STEALTH_SCRIPT = """
    // Hide webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    // Fake plugins (headless has 0)
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5]
    });
    // Fake languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en']
    });
    // Fix chrome.runtime (missing in automation)
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) window.chrome.runtime = {};
    // Permissions query override
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(params);
    // WebGL vendor/renderer spoofing
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Intel Inc.';
        if (param === 37446) return 'Intel Iris OpenGL Engine';
        return getParam.call(this, param);
    };
    """

    # ── Challenge detection ──────────────────────────────────────────────────

    _CHALLENGE_PATTERNS = [
        'checking your browser',
        'just a moment',
        'verify you are human',
        'cloudflare',
        'captcha',
        'challenge-platform',
        'access denied',
        'please wait',
        'bot detection',
        'are you a robot',
    ]

    async def _detect_challenge(self, page):
        """Check if the current page is a bot challenge / CAPTCHA."""
        try:
            title = (await page.title() or '').lower()
            # Check title
            for pat in self._CHALLENGE_PATTERNS:
                if pat in title:
                    return True
            # Check body text (first 2000 chars to be fast)
            body = await page.evaluate(
                "(document.body && document.body.innerText || '').substring(0, 2000).toLowerCase()")
            for pat in self._CHALLENGE_PATTERNS:
                if pat in body:
                    return True
            # Check for Cloudflare-specific elements
            cf = await page.query_selector('#challenge-form, #cf-challenge-running, .cf-browser-verification')
            if cf:
                return True
        except Exception as e:
            self.log(f"Challenge detection error: {e}", "DEBUG")
        return False

    async def _handle_challenge(self, page, browser, pw):
        """
        When a challenge is detected:
        1. Log a warning
        2. If headless, relaunch browser in visible mode for manual solve
        3. Wait for the challenge to clear (poll every 2s, up to 5 min)
        4. Return True if solved, False if timed out
        """
        self.log("Bot challenge detected — waiting for clearance...", "WARN")
        self.status_signal.emit("challenge")

        # If running headless, we can't solve CAPTCHAs — inform user
        if self.cfg.get('headless', True):
            self.log("Run with Headless OFF to solve challenges manually.", "ERROR")
            self.log("Pausing 60s before retry...", "WARN")
            for _ in range(60):
                if self._stop.is_set(): return False
                await asyncio.sleep(1)
            return False

        # Visible mode: wait for user to solve (poll for challenge gone)
        self.log("Solve the challenge in the browser window...", "WARN")
        timeout = 300  # 5 minutes max
        for elapsed in range(timeout):
            if self._stop.is_set(): return False
            await asyncio.sleep(2)
            if not await self._detect_challenge(page):
                self.log("Challenge cleared!", "OK")
                self.status_signal.emit("running")
                # Extra settle time after challenge
                await asyncio.sleep(3)
                return True
        self.log("Challenge timeout (5 min) — skipping page", "ERROR")
        return False

    # ── Main crawl loop ─────────────────────────────────────────────────────

    async def _crawl(self):
        from playwright.async_api import async_playwright

        headless = self.cfg.get('headless', True)
        self.log(f"Launching Chromium ({'headless' if headless else 'visible'})...", "INFO")

        # Session persistence: reuse browser profile for cookies/localStorage
        profile_dir = os.path.join(get_config_dir(), 'browser_profile')
        os.makedirs(profile_dir, exist_ok=True)

        # Rotate through recent UA strings
        ua_versions = ['131.0.0.0', '130.0.0.0', '129.0.0.0', '128.0.0.0']
        ua_ver = random.choice(ua_versions)
        ua = f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ua_ver} Safari/537.36'

        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                profile_dir,
                headless=headless,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                    '--disable-dev-shm-usage',
                    f'--window-size=1440,900',
                ],
                viewport={'width': 1440, 'height': 900},
                user_agent=ua,
                locale='en-US',
                timezone_id='America/New_York',
                extra_http_headers={
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                    'sec-ch-ua': f'"Chromium";v="{ua_ver.split(".")[0]}", "Google Chrome";v="{ua_ver.split(".")[0]}", "Not?A_Brand";v="99"',
                    'sec-ch-ua-mobile': '?0',
                    'sec-ch-ua-platform': '"Windows"',
                },
                ignore_default_args=['--enable-automation'],
            )

            # Inject stealth patches into every new page/frame
            await context.add_init_script(self.STEALTH_SCRIPT)

            # Only block heavy .ts video segments — let everything else through
            await context.route('**/*', self._route_handler)

            page = context.pages[0] if context.pages else await context.new_page()

            # ── Seed all active profiles ──────────────────────────────────
            for _prof in self._profiles:
                if len(self._profiles) == 1:
                    _start = self._normalize_url(
                        self.cfg.get('start_url', '') or _prof.start_url)
                else:
                    _start = self._normalize_url(_prof.start_url)
                if _start:
                    self.db.execute("DELETE FROM crawled_pages WHERE url=?", (_start,))
                    self.db.conn.commit()
                    self.db.enqueue(_start, 0, 100, profile=_prof.name)
                    self.log(f"Seeded [{_prof.name}]: {_start}", "INFO")

            prof_names = ', '.join(p.name for p in self._profiles)
            self.log(f"Profiles: {prof_names}  |  Batch: {self._batch_size} pages each", "INFO")
            self.status_signal.emit("running")
            self.stats_signal.emit(self.db.stats())

            challenge_backoff = 1.0
            page_count = 0
            profile_idx = 0
            batch_size = self._batch_size

            while not self._stop.is_set():
                # ── Round-robin: try each profile in turn ─────────────────
                empty_profiles = 0
                for _ in range(len(self._profiles)):
                    if self._stop.is_set(): break

                    # Switch to next profile
                    self.profile = self._profiles[profile_idx % len(self._profiles)]
                    self._video_re = self.profile.get_combined_video_re()
                    profile_idx += 1
                    pname = self.profile.name

                    pq = self.db.queue_size(profile=pname)
                    if pq == 0:
                        empty_profiles += 1
                        continue

                    self.log(f"--- [{pname}] Starting batch ({pq} queued) ---", "INFO")
                    batch_count = 0

                    while batch_count < batch_size and not self._stop.is_set():
                        while self._pause.is_set() and not self._stop.is_set():
                            await asyncio.sleep(0.5)
                        if self._stop.is_set(): break

                        item = self.db.dequeue(profile=pname)
                        if not item: break

                        url, depth = item['url'], item['depth']

                        _parsed = urlparse(url)
                        if not self.profile.is_allowed_domain(_parsed.netloc):
                            self.db.mark_processed(url, depth)
                            self.log(f"SKIP (domain): {url[:80]}", "DEBUG")
                            continue
                        if self.profile.is_excluded(url):
                            self.db.mark_processed(url, depth)
                            self.log(f"SKIP (excluded): {url[:80]}", "DEBUG")
                            continue

                        max_p = self.cfg.get('max_pages', 0)
                        if max_p > 0 and page_count >= max_p:
                            self.log(f"Max pages ({max_p}) reached", "WARN"); break

                        if self.cfg.get('resume', True) and self.db.is_processed(url):
                            self.log(f"SKIP (already processed): {url[:80]}", "DEBUG"); continue

                        is_clip = self._is_clip(url)
                        is_cat = self._is_catalog(url)
                        page_type = 'CLIP' if is_clip else ('CATALOG' if is_cat else 'GENERIC')
                        self.log(f"[{pname}] DEQUEUE [{page_type}] d{depth} p{page_count} {url[:80]}", "INFO")

                        if is_clip:
                            await self._crawl_clip(context, url, depth)
                        elif is_cat:
                            await self._crawl_catalog(page, url, depth)
                        else:
                            await self._crawl_clip(context, url, depth)

                        if await self._detect_challenge(page):
                            solved = await self._handle_challenge(page, None, pw)
                            if not solved:
                                self.db.enqueue(url, depth, 10, profile=pname)
                                challenge_backoff = min(challenge_backoff * 2, 8.0)
                                self.log(f"Backoff multiplier: {challenge_backoff:.1f}x", "WARN")
                                continue
                            else:
                                challenge_backoff = max(challenge_backoff * 0.7, 1.0)

                        page_count += 1
                        batch_count += 1
                        self.stats_signal.emit(self.db.stats())

                        delay = self.cfg.get('page_delay', 2500)
                        jitter = random.uniform(0.6, 1.5)
                        wait = delay * jitter * challenge_backoff
                        await asyncio.sleep(max(0.5, wait / 1000))

                    if batch_count > 0:
                        self.log(
                            f"--- [{pname}] Batch done: {batch_count} pages, rotating ---",
                            "INFO")

                # If ALL profiles' queues are empty, we're done
                if empty_profiles >= len(self._profiles):
                    total_q = self.db.queue_size()
                    if total_q == 0:
                        self.log("All queues empty -- crawl complete!", "OK")
                        break

            await context.close()

        self.status_signal.emit("stopped")
        self.stats_signal.emit(self.db.stats())
        self.finished.emit()

    async def _route_handler(self, route):
        url = route.request.url.lower()
        # ONLY block heavy HLS .ts video segments (multi-MB each).
        # Let EVERYTHING else through — blocking images/fonts/CSS is a
        # major anti-bot detection signal that Cloudflare catches instantly.
        # NOTE: Parenthesized to fix operator precedence (and > or), and
        # tightened regex to only match .ts file extensions, not query params.
        if (url.endswith('.ts') and '/segment' in url) or re.search(r'/[^/]+\.ts\?', url):
            await route.abort()
        else:
            await route.continue_()

    # ── Response interception ────────────────────────────────────────────────

    async def _on_response(self, response, source_url, clip_meta):
        url = response.url

        # Capture direct video URL requests (M3U8, MP4, WebM, etc.)
        if self._video_re.search(url):
            # On clip pages, only record URLs matching the current clip's video ID
            # to avoid capturing SD preview thumbnails for 150+ related videos
            current_id = clip_meta.get('clip_id', '')
            if current_id:
                vid_m = re.search(r'/video-files/(\d+)/', url)
                if vid_m and vid_m.group(1) != current_id:
                    return  # Skip — this is a related video's preview, not our clip
            await self._record_video_url(url.strip(), source_url, clip_meta)
            return

        # For body scanning: only small JSON responses (API calls that may embed URLs)
        rt = response.request.resource_type
        if rt not in ('xhr', 'fetch'):
            return
        try:
            ct = response.headers.get('content-type', '')
            if 'json' not in ct:
                return
            cl = int(response.headers.get('content-length', '0') or 0)
            if cl > 512_000:
                return
            body = await response.text()
            for m in self._video_re.findall(body):
                await self._record_video_url(m.strip(), source_url, clip_meta)
        except Exception as e:
            # Don't log for common non-errors (binary responses, connection resets)
            err = str(e)
            if not any(x in err for x in ('decode', 'Connection', 'Target closed', 'disposed')):
                self.log(f"Response scan error [{source_url[-40:]}]: {err[:80]}", "DEBUG")

    async def _record_video_url(self, url, source_url, clip_meta):
        """Record a discovered video URL (M3U8, MP4, WebM, etc.) into the database.
        Handles quality upgrades: if a better URL is found for an existing clip, replaces it.
        """
        if not url or not self._video_re.search(url):
            return
        url = url.rstrip('"\'\\')

        # Filter by CDN domain if profile specifies one
        if self.profile.video_cdn_domain:
            if self.profile.video_cdn_domain not in urlparse(url).netloc:
                self.log(f"  [skip] CDN domain mismatch: {url[:80]}", "DEBUG")
                return

        meta = dict(clip_meta)
        meta['m3u8_url']   = url
        meta['source_url'] = source_url or meta.get('source_url', '')

        # ── Auto-extract metadata from video URL filename ─────────────
        vid_id_m = re.search(r'/video-files/(\d+)/', url)
        if vid_id_m:
            meta['clip_id'] = vid_id_m.group(1)
        res_m = re.search(r'(\d{3,4})_(\d{3,4})_(\d+)fps', url)
        quality_label = '?'
        if res_m:
            w, h = int(res_m.group(1)), int(res_m.group(2))
            fps = res_m.group(3)
            meta['resolution'] = f"{w}x{h}"
            meta['frame_rate'] = fps
            quality_label = f"{max(w,h)}p"
        qual_m = re.search(r'-(uhd|hd|sd)_', url, re.IGNORECASE)
        if qual_m:
            meta['formats'] = qual_m.group(1).upper()
            quality_label = qual_m.group(1).upper()

        # Determine type label for log
        ext = 'VIDEO'
        for t in ('m3u8','mp4','webm','mpd','mov'):
            if f'.{t}' in url.lower():
                ext = t.upper(); break
        clip_id = meta.get('clip_id', '')

        # ── Try INSERT first ──────────────────────────────────────────
        is_new = self.db.save_clip(meta)
        if is_new:
            self.log(
                f"  [NEW] {ext} {quality_label} | id:{clip_id} | "
                f"{meta.get('title','')[:30] or '(no title)'} | {url[:70]}",
                "M3U8")
            return

        # ── Existing clip — try quality upgrade ───────────────────────
        if clip_id:
            result = self.db.update_m3u8(clip_id, url)
            if result == 'upgraded':
                self.log(
                    f"  [UPGRADED] {ext} {quality_label} | id:{clip_id} | {url[:70]}",
                    "M3U8")
            elif result == 'same':
                pass
            elif result == 'kept_existing':
                self.log(
                    f"  [skip] {quality_label} <= existing | id:{clip_id} | {url[:70]}",
                    "DEBUG")
            elif result == 'set_new':
                self.log(
                    f"  [SET] {ext} {quality_label} | id:{clip_id} | {url[:70]}",
                    "M3U8")

    # Backward compat alias
    async def _record_m3u8(self, url, source_url, clip_meta):
        await self._record_video_url(url, source_url, clip_meta)

    # ── Metadata extraction from DOM ─────────────────────────────────────────

    async def _extract_metadata(self, page, url) -> dict:
        """
        Profile-driven metadata extraction with generic fallbacks.

        Strategy:
        1. Always try OpenGraph meta tags (universal)
        2. Always try JSON-LD structured data
        3. Apply profile-specific regex selectors to body text
        4. Fallback to h1 / page title
        """
        meta = {k: '' for k in ('clip_id','source_url','title','creator','collection',
                                 'resolution','duration','frame_rate','camera',
                                 'formats','tags','thumbnail_url','m3u8_url','source_site')}
        meta['source_url'] = url
        meta['source_site'] = self.profile.name

        try:
            # ── Clip/Item ID from URL (numeric final segment) ─────────────
            m = re.search(r'/(\d{4,})(?:/|$)', url)
            if m: meta['clip_id'] = m.group(1)

            # ── OpenGraph meta tags (works on most sites) ─────────────────
            if self.profile.og_fallback:
                og_vals = {}
                for prop in ('og:image','og:title','og:description','og:video','og:video:url','og:url'):
                    try:
                        el = await page.query_selector(f'meta[property="{prop}"]')
                        if not el: el = await page.query_selector(f'meta[name="{prop}"]')
                        if el:
                            og_vals[prop] = (await el.get_attribute('content') or '').strip()
                    except Exception: pass

                # Thumbnail
                if og_vals.get('og:image') and not meta['thumbnail_url']:
                    meta['thumbnail_url'] = og_vals['og:image']

                # Video URL
                for vk in ('og:video:url', 'og:video'):
                    if og_vals.get(vk) and not meta['m3u8_url']:
                        meta['m3u8_url'] = og_vals[vk]

                # Title — smart extraction
                og_title = og_vals.get('og:title', '')
                if og_title and not meta['title']:
                    # Pexels: "Video by Author on Pexels" → extract author, use URL slug for title
                    author_m = re.match(r'(?:Video|Photo)\s+by\s+(.+?)\s+on\s+Pexels', og_title, re.IGNORECASE)
                    if author_m:
                        if not meta['creator']:
                            meta['creator'] = author_m.group(1).strip()
                        # Build title from URL slug instead
                        slug_m = re.search(r'/video/([^/]+)-\d+/?$', url)
                        if slug_m:
                            meta['title'] = slug_m.group(1).replace('-', ' ').title()
                    else:
                        meta['title'] = og_title

                # Description → extract creator + tags
                og_desc = og_vals.get('og:description', '')
                if og_desc:
                    # "Download this video by Author for free on Pexels"
                    desc_author = re.search(r'by\s+(\w[\w\s.]+?)(?:\s+for\s+free|\s+on\s+|\s*$)', og_desc, re.IGNORECASE)
                    if desc_author and not meta['creator']:
                        meta['creator'] = desc_author.group(1).strip()
                    # For non-Pexels sites, description might contain useful keywords
                    if not meta['tags'] and 'pexels.com' not in url:
                        meta['tags'] = og_desc[:300]

                # Clip ID from og:url if not already extracted
                if og_vals.get('og:url') and not meta['clip_id']:
                    id_m = re.search(r'/(\d{4,})/?$', og_vals['og:url'])
                    if id_m: meta['clip_id'] = id_m.group(1)

            # ── JSON-LD structured data ───────────────────────────────────
            if self.profile.jsonld_fallback:
                try:
                    jsonld = await page.evaluate("""
                        (() => {
                            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
                            for (const s of scripts) {
                                try {
                                    const d = JSON.parse(s.textContent);
                                    if (d['@type'] === 'VideoObject' || d['@type'] === 'ImageObject')
                                        return d;
                                    if (Array.isArray(d['@graph'])) {
                                        const v = d['@graph'].find(i => i['@type'] === 'VideoObject');
                                        if (v) return v;
                                    }
                                } catch(e) {}
                            }
                            return null;
                        })()
                    """)
                    if jsonld:
                        if not meta['title'] and jsonld.get('name'):
                            meta['title'] = str(jsonld['name'])[:200]
                        if not meta['thumbnail_url'] and jsonld.get('thumbnailUrl'):
                            meta['thumbnail_url'] = str(jsonld['thumbnailUrl'])
                        if not meta['duration'] and jsonld.get('duration'):
                            meta['duration'] = str(jsonld['duration'])
                        if not meta['creator'] and jsonld.get('author'):
                            a = jsonld['author']
                            meta['creator'] = str(a.get('name','') if isinstance(a, dict) else a)[:100]
                        if not meta['tags'] and jsonld.get('keywords'):
                            kw = jsonld['keywords']
                            meta['tags'] = kw if isinstance(kw, str) else ', '.join(kw[:25])
                        # Video content URL
                        if not meta['m3u8_url'] and jsonld.get('contentUrl'):
                            meta['m3u8_url'] = str(jsonld['contentUrl'])
                except Exception: pass

            # ── Profile-specific regex selectors on body text ─────────────
            if self.profile.metadata_selectors:
                body_text = await page.evaluate("document.body ? document.body.innerText : ''")
                for field, pat in self.profile.metadata_selectors.items():
                    if meta.get(field): continue  # don't overwrite existing
                    try:
                        if field == 'tags':
                            # Special tags extraction (multi-line)
                            tags_m = re.search(pat, body_text, re.IGNORECASE)
                            if tags_m:
                                raw = tags_m.group(1)
                                tags = [t.strip() for t in re.split(r'\n|  +', raw)
                                        if t.strip() and 1 < len(t.strip()) < 35
                                        and not re.match(r'^https?://', t.strip())]
                                meta['tags'] = ', '.join(tags[:25])
                        else:
                            m2 = re.search(pat, body_text, re.IGNORECASE)
                            if m2:
                                meta[field] = m2.group(1).strip()
                    except Exception: pass

            # ── Title fallback: h1 → URL slug → page title ──────────────
            if not meta['title']:
                try:
                    h1 = await page.query_selector('h1')
                    if h1:
                        h1_text = (await h1.inner_text()).strip()
                        # Skip generic site-wide h1s (Pexels, Pixabay catalog headings)
                        generic_h1 = re.match(
                            r'(The best free|Free stock|Download free|Search results|Browse)',
                            h1_text, re.IGNORECASE)
                        if not generic_h1 and len(h1_text) > 3:
                            meta['title'] = h1_text
                except Exception: pass
            # URL slug fallback (e.g. /video/freelander-road-trip-at-spitzkoppe-18010808/)
            if not meta['title']:
                slug_m = re.search(r'/(?:video|clip|stock-footage)/([^/]+?)(?:-\d+)?/?$', url)
                if slug_m:
                    meta['title'] = slug_m.group(1).replace('-', ' ').title()
            if not meta['title']:
                pt = await page.title()
                meta['title'] = re.sub(
                    r'\s*[|–-]\s*(Stock Footage|Artlist|Pexels|Pixabay|Storyblocks|Free).*$',
                    '', pt, flags=re.IGNORECASE).strip()

            # ── Post-processing ───────────────────────────────────────────
            if meta['resolution']:
                meta['resolution'] = re.sub(r'\s*[xX\u00d7]\s*', 'x', meta['resolution'])
            if meta['formats']:
                fmts = re.findall(r'4K|2K|HD|SD|ProRes|MP4|MOV|RAW|WebM', meta['formats'], re.IGNORECASE)
                meta['formats'] = ', '.join(fmts) if fmts else meta['formats'][:60].strip()

            for k in meta:
                if isinstance(meta[k], str):
                    meta[k] = meta[k].strip()[:500]

        except Exception as e:
            self.log(f"Metadata error on {url}: {e}", "WARN")

        return meta

    # ── Page crawl strategies ────────────────────────────────────────────────

    async def _crawl_catalog(self, page, url, depth):
        self.log(f"CATALOG [d{depth}] {url}", "INFO")

        try:
            if not await self._safe_goto(page, url):
                self.db.mark_failed(url, depth); return

            # Randomized settle time for React/Next.js render
            await asyncio.sleep(random.uniform(2.0, 4.0))

            # ── Load More loop: click the button to expand catalog ────────
            load_more = self.profile.load_more_selector
            max_clicks = self.profile.load_more_clicks
            if load_more and max_clicks > 0:
                for click_i in range(max_clicks):
                    if self._stop.is_set():
                        break
                    try:
                        btn = await page.query_selector(load_more)
                        if not btn:
                            break
                        visible = await btn.is_visible()
                        if not visible:
                            break
                        await btn.scroll_into_view_if_needed()
                        await asyncio.sleep(random.uniform(0.3, 0.8))
                        await btn.click()
                        self.log(f"  Load More click {click_i+1}/{max_clicks}", "INFO")
                        await asyncio.sleep(random.uniform(1.5, 3.0))
                    except Exception:
                        break

            await self._scroll_to_bottom(page)

            # ── Extract video MP4s directly from catalog page ─────────────
            # Pexels embeds SD MP4 URLs in <video src="..."> on catalog pages
            await self._extract_catalog_videos(page, url)

            # ── Extract and queue all item/catalog links ──────────────────
            links = await self._extract_links(page)
            queued = 0
            seen = set()
            resume = self.cfg.get('resume', True)
            for link in links:
                norm = self._normalize_url(link)
                if not norm or norm in seen or self._is_excluded(norm): continue
                seen.add(norm)
                if self._is_clip(norm):
                    if not (resume and self.db.is_processed(norm)):
                        self.db.enqueue(norm, depth+1, 10, profile=self.profile.name); queued += 1
                elif depth < self.cfg.get('max_depth', 2) and self._is_catalog(norm):
                    if not (resume and self.db.is_processed(norm)):
                        self.db.enqueue(norm, depth+1, 5, profile=self.profile.name)

            self.db.mark_processed(url, depth)
            self.log(f"CATALOG done — {queued} items queued  (depth {depth})", "OK")
        except Exception as e:
            self.log(f"CATALOG error: {e}", "ERROR")
            self.db.mark_failed(url, depth)

    async def _extract_catalog_videos(self, page, source_url):
        """Extract video URLs directly from catalog pages (e.g. Pexels <video> tags)."""
        try:
            results = await page.evaluate("""
                (() => {
                    const out = [];
                    document.querySelectorAll('video[src]').forEach(v => {
                        const src = v.src || v.getAttribute('src') || '';
                        if (src && src.startsWith('http')) {
                            const link = v.closest('a[href]');
                            const href = link ? link.href : '';
                            out.push({src, href});
                        }
                    });
                    document.querySelectorAll('video source[src]').forEach(s => {
                        const src = s.src || s.getAttribute('src') || '';
                        if (src && src.startsWith('http')) {
                            const link = s.closest('a[href]');
                            out.push({src, href: link ? link.href : ''});
                        }
                    });
                    return out;
                })()
            """)
            total = len(results or [])
            self.log(f"  [catalog-extract] Found {total} <video> elements on page", "INFO")
            count = 0
            for item in (results or []):
                src = item.get('src', '')
                if src and self._video_re.search(src):
                    vid_m = re.search(r'/video-files/(\d+)/', src)
                    meta = {k: '' for k in ('clip_id','source_url','title','creator','collection',
                                             'resolution','duration','frame_rate','camera',
                                             'formats','tags','thumbnail_url','m3u8_url','source_site')}
                    meta['source_url'] = item.get('href', '') or source_url
                    meta['source_site'] = self.profile.name
                    meta['m3u8_url'] = src
                    if vid_m:
                        meta['clip_id'] = vid_m.group(1)
                    res_m = re.search(r'(\d{3,4})_(\d{3,4})_(\d+)fps', src)
                    if res_m:
                        meta['resolution'] = f"{res_m.group(1)}x{res_m.group(2)}"
                        meta['frame_rate'] = res_m.group(3)
                    is_new = self.db.save_clip(meta)
                    if is_new:
                        count += 1
                        self.log(
                            f"  [catalog-extract] NEW id:{meta.get('clip_id','')} "
                            f"{meta.get('resolution','?')} {src[-50:]}", "M3U8")
            self.log(f"  [catalog-extract] {count} new / {total} total videos", "OK" if count else "INFO")
        except Exception as e:
            self.log(f"  [catalog-extract] Error: {e}", "WARN")

    async def _crawl_clip(self, context, url, depth):
        """
        Crawl one clip page in a DEDICATED browser page (tab).
        After extracting metadata + video URLs, scrolls to bottom to discover
        Related/Similar clip links — creating exponential link growth.
        """
        _pre_id = re.search(r'/(\d{4,})(?:/|$)', url)
        clip_id_preview = _pre_id.group(1) if _pre_id else '?'
        self.log(f"CLIP    [d{depth}] id:{clip_id_preview} {url}", "INFO")

        clip_meta = {k: '' for k in ('clip_id','source_url','title','creator','collection',
                                       'resolution','duration','frame_rate','camera',
                                       'formats','tags','thumbnail_url','m3u8_url','source_site')}
        clip_meta['source_url'] = url
        clip_meta['source_site'] = self.profile.name
        if _pre_id:
            clip_meta['clip_id'] = _pre_id.group(1)

        page = await context.new_page()
        try:
            async def on_resp(resp):
                try:
                    await self._on_response(resp, url, clip_meta)
                except Exception as e:
                    err = str(e)
                    if not any(x in err for x in ('Target closed', 'disposed', 'Connection')):
                        self.log(f"Resp handler error: {err[:80]}", "DEBUG")
            page.on('response', on_resp)

            self.log(f"  [1/6] Loading page...", "DEBUG")
            if not await self._safe_goto(page, url):
                self.log(f"  [FAIL] Page load failed: {url}", "ERROR")
                self.db.mark_failed(url, depth); return

            # Randomized settle for React hydration
            await asyncio.sleep(random.uniform(1.5, 3.0))
            try:
                await page.wait_for_selector(
                    '[class*="clipDetail"], [class*="clip-detail"], h1, '
                    '[class*="title"], [class*="VideoPlayer"], [class*="MediaPlayer"], '
                    'video[src], [class*="DownloadButton"]',
                    timeout=8000)
            except (TimeoutError, Exception) as e:
                if 'Timeout' not in type(e).__name__:
                    self.log(f"Selector wait error: {e}", "DEBUG")

            # Extract metadata
            self.log(f"  [2/6] Extracting metadata...", "DEBUG")
            extracted = await self._extract_metadata(page, url)
            filled = [k for k, v in extracted.items() if v]
            for k, v in extracted.items():
                if v and not clip_meta.get(k):
                    clip_meta[k] = v
            self.log(
                f"  [2/6] Metadata: {len(filled)} fields filled "
                f"(title:{'yes' if extracted.get('title') else 'no'} "
                f"thumb:{'yes' if extracted.get('thumbnail_url') else 'no'} "
                f"video:{'yes' if extracted.get('m3u8_url') else 'no'})",
                "DEBUG")

            # Save/backfill record with full metadata
            if clip_meta.get('clip_id') or clip_meta.get('title'):
                self.db.save_clip(clip_meta)
            if clip_meta.get('clip_id'):
                self.db.update_metadata(clip_meta['clip_id'], clip_meta)
            if clip_meta.get('title') or clip_meta.get('m3u8_url'):
                pass  # clip_signal emitted ONCE at end after full scan for best quality

            # Hover to trigger the HLS player
            self.log(f"  [3/6] Triggering video players...", "DEBUG")
            await self._trigger_players(page)
            await asyncio.sleep(self.cfg.get('m3u8_wait', 4000) / 1000)

            # Scan page HTML for video URLs
            self.log(f"  [4/6] Scanning page source for video URLs...", "DEBUG")
            await self._scan_page_source(page, url, clip_meta)
            await self._collect_js_m3u8s(page, url, clip_meta)

            # Scroll down to expose Related/Similar sections
            self.log(f"  [5/6] Scrolling for related content...", "DEBUG")
            await self._scroll_to_bottom(page)
            await asyncio.sleep(random.uniform(1.0, 2.0))

            # Extract all links from the clip page
            self.log(f"  [6/6] Extracting links...", "DEBUG")
            links = await self._extract_links(page)
            queued = 0
            skipped_processed = 0
            skipped_excluded = 0
            seen = set()
            resume = self.cfg.get('resume', True)
            for link in links:
                norm = self._normalize_url(link)
                if not norm or norm in seen: continue
                if norm == url: continue
                seen.add(norm)
                if self._is_excluded(norm):
                    skipped_excluded += 1; continue
                if self._is_clip(norm):
                    if resume and self.db.is_processed(norm):
                        skipped_processed += 1; continue
                    self.db.enqueue(norm, depth+1, 10, profile=self.profile.name)
                    queued += 1
                elif self._is_catalog(norm) and depth < self.cfg.get('max_depth', 2):
                    if not (resume and self.db.is_processed(norm)):
                        self.db.enqueue(norm, depth+1, 5, profile=self.profile.name)

            # Persist final state
            if clip_meta.get('clip_id'):
                self.db.update_metadata(clip_meta['clip_id'], clip_meta)
                # Also try to upgrade video URL from the metadata
                if clip_meta.get('m3u8_url'):
                    self.db.update_m3u8(clip_meta['clip_id'], clip_meta['m3u8_url'])
            elif clip_meta.get('title') or clip_meta.get('m3u8_url'):
                self.db.save_clip(clip_meta)

            self.db.mark_processed(url, depth)

            # ── Emit clip_signal ONCE with fresh DB data (best URL + full metadata) ──
            if clip_meta.get('clip_id'):
                _fresh = self.db.execute(
                    "SELECT * FROM clips WHERE clip_id=?", (clip_meta['clip_id'],)).fetchone()
                if _fresh and _fresh['m3u8_url']:
                    _emit_data = dict(zip(_fresh.keys(), tuple(_fresh)))
                    self.clip_signal.emit(_emit_data)

            # ── Final summary log ─────────────────────────────────────────
            n_tags = len([t for t in clip_meta.get('tags','').split(',') if t.strip()])
            has_video = bool(clip_meta.get('m3u8_url'))
            if not has_video and clip_meta.get('clip_id'):
                row = self.db.execute(
                    "SELECT m3u8_url FROM clips WHERE clip_id=?", (clip_meta['clip_id'],)).fetchone()
                has_video = bool(row and row['m3u8_url'])
            self.log(
                f"CLIP done id:{clip_meta.get('clip_id','?')} "
                f"'{clip_meta.get('title','?')[:30]}' "
                f"res:{clip_meta.get('resolution','?') or '?'} "
                f"{'VIDEO OK' if has_video else 'NO VIDEO'} "
                f"+{queued} queued ({skipped_processed} already done, {skipped_excluded} excluded)",
                "OK" if has_video else "WARN")
            self.stats_signal.emit(self.db.stats())

        except Exception as e:
            import traceback as _tb
            self.log(f"CLIP error [{url[-50:]}]: {e}\n{_tb.format_exc()[:400]}", "ERROR")
            self.db.mark_failed(url, depth)
        finally:
            try:
                await page.close()
            except (Exception) as e:
                if 'Target closed' not in str(e) and 'disposed' not in str(e):
                    self.log(f"Page close error: {e}", "DEBUG")

    async def _scan_page_source(self, page, source_url, clip_meta):
        """
        Scan page HTML for video URLs using multiple strategies:
        1. Regex match all video URLs in raw HTML
        2. Extract src/data-src from <video> and <source> DOM elements
        3. Extract HD/UHD MP4s from Canva partner links (Pexels-specific)
        4. If multiple qualities found for same video ID, prefer highest resolution
        
        IMPORTANT: On clip pages, only records URLs for the CURRENT clip's video ID
        to avoid capturing SD previews for 150+ related videos on the same page.
        """
        try:
            html = await page.content()
            current_clip_id = clip_meta.get('clip_id', '')
            found_urls = set()

            # ── Strategy 1: Regex scan raw HTML ──────────────────────────
            regex_hits = self._video_re.findall(html)
            for m in regex_hits:
                found_urls.add(m.strip())
            if regex_hits:
                self.log(f"  [scan] HTML regex: {len(regex_hits)} video URLs in source", "DEBUG")

            # ── Strategy 2: DOM video/source elements ────────────────────
            vid_srcs = await page.evaluate("""
                [...document.querySelectorAll('video[src], source[src], video source[src]')]
                    .map(el => el.src || el.getAttribute('src') || el.getAttribute('data-src') || '')
                    .filter(s => s && s.startsWith('http'))
            """)
            dom_count = 0
            for src in (vid_srcs or []):
                if self._video_re.search(src):
                    found_urls.add(src.strip())
                    dom_count += 1
            if dom_count:
                self.log(f"  [scan] DOM elements: {dom_count} video src attributes", "DEBUG")

            # ── Strategy 3: Canva partner links (Pexels HD/UHD) ──────────
            canva_urls = re.findall(
                r'file-url=(https?%3A%2F%2F[^&"\'<>\s]+\.mp4[^&"\'<>\s]*)', html)
            canva_count = 0
            for encoded in canva_urls:
                decoded = unquote(encoded)
                if self._video_re.search(decoded):
                    found_urls.add(decoded)
                    canva_count += 1
            if canva_count:
                self.log(f"  [scan] Canva partner links: {canva_count} HD/UHD MP4s", "DEBUG")

            # ── Group by video ID and filter ─────────────────────────────
            by_vid_id = {}
            for u in found_urls:
                vid_m = re.search(r'/video-files/(\d+)/', u)
                vid_id = vid_m.group(1) if vid_m else '__unknown__'
                by_vid_id.setdefault(vid_id, []).append(u)

            total_ids = len(by_vid_id)

            # On clip pages: only record URLs matching our clip's video ID
            # This prevents capturing SD preview MP4s for 150+ related videos
            if current_clip_id and current_clip_id in by_vid_id:
                my_urls = by_vid_id[current_clip_id]
                skipped = total_ids - 1
                self.log(
                    f"  [scan] Total: {len(found_urls)} URLs across {total_ids} video IDs. "
                    f"Filtering to clip id:{current_clip_id} ({len(my_urls)} URLs, "
                    f"skipped {skipped} other videos' previews)",
                    "INFO")
                best = self._pick_best_quality(my_urls)
                if len(my_urls) > 1:
                    self.log(
                        f"  [scan] id:{current_clip_id} — {len(my_urls)} qualities, "
                        f"best: {best.split('/')[-1][:60]}",
                        "DEBUG")
                await self._record_video_url(best, source_url, clip_meta)
            elif current_clip_id:
                # Our clip ID wasn't found in any video URL — try unknown bucket
                self.log(
                    f"  [scan] WARNING: clip id:{current_clip_id} not found in "
                    f"{total_ids} video IDs. Recording all {len(found_urls)} URLs.",
                    "WARN")
                for vid_id, urls in by_vid_id.items():
                    best = self._pick_best_quality(urls)
                    await self._record_video_url(best, source_url, clip_meta)
            else:
                # No clip ID context (catalog page or generic) — record all
                self.log(
                    f"  [scan] No clip ID filter — recording {total_ids} unique videos "
                    f"({len(found_urls)} total URLs)",
                    "INFO")
                for vid_id, urls in by_vid_id.items():
                    best = self._pick_best_quality(urls)
                    await self._record_video_url(best, source_url, clip_meta)

        except Exception as e:
            self.log(f"  [scan] Error: {e}", "WARN")

    @staticmethod
    def _pick_best_quality(urls):
        """Pick highest resolution video URL from a list of the same video."""
        if len(urls) <= 1:
            return urls[0] if urls else ''
        def _score(u):
            # uhd > hd > sd; higher resolution = higher score
            m = re.search(r'(\d{3,4})_(\d{3,4})_(\d+)fps', u)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                return max(w, h)
            if 'uhd' in u: return 3000
            if '-hd_' in u: return 1500
            if '-sd_' in u: return 500
            return 100
        return max(urls, key=_score)

    VIDEO_INTERCEPT_SCRIPT = """
        (function() {
            var VIDEO_EXTS = /\\.(m3u8|mp4|webm|mpd|m3u|mov)(\\?|$)/i;
            window.__interceptedVideoUrls__ = window.__interceptedVideoUrls__ || [];
            // Intercept XMLHttpRequest
            var _XHRopen = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function(method, url) {
                if (typeof url === 'string' && VIDEO_EXTS.test(url)) {
                    window.__interceptedVideoUrls__.push(url);
                }
                return _XHRopen.apply(this, arguments);
            };
            // Intercept fetch()
            var _fetch = window.fetch;
            window.fetch = function(input) {
                var url = typeof input === 'string' ? input : (input && input.url) || '';
                if (VIDEO_EXTS.test(url)) {
                    window.__interceptedVideoUrls__.push(url);
                }
                return _fetch.apply(this, arguments);
            };
            // Observe <video>/<source> src changes
            var mo = new MutationObserver(function(muts) {
                muts.forEach(function(m) {
                    m.addedNodes.forEach(function(n) {
                        if (!n.querySelectorAll) return;
                        n.querySelectorAll('video[src], source[src], video source[src]').forEach(function(el) {
                            var s = el.src || el.getAttribute('src') || '';
                            if (s && s.startsWith('http')) window.__interceptedVideoUrls__.push(s);
                        });
                    });
                });
            });
            mo.observe(document.documentElement, {childList:true, subtree:true});
        })();
    """

    async def _safe_goto(self, page, url):
        timeout = self.cfg.get('timeout', 30000)
        # Inject XHR/fetch interceptor + stealth BEFORE any page JS runs.
        await page.add_init_script(self.VIDEO_INTERCEPT_SCRIPT)

        # domcontentloaded is the only safe wait_until for Next.js SPAs.
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=timeout)
            return True
        except Exception as e:
            err = str(e)
            if any(x in err for x in ('ERR_ABORTED', 'Navigation', 'net::ERR')):
                try:
                    if await page.title():
                        return True
                except (Exception,):
                    pass  # Page truly failed — fall through to error log
            self.log(f"goto failed: {err[:120]}", "ERROR")
            return False

    async def _scroll_to_bottom(self, page):
        steps = self.cfg.get('scroll_steps', 15)
        base_delay = self.cfg.get('scroll_delay', 800) / 1000
        progress = 0.0
        for i in range(steps):
            if self._stop.is_set(): break
            # Variable scroll increments — sometimes big, sometimes small
            increment = random.uniform(0.04, 0.12)
            progress = min(progress + increment, 1.0)
            # Occasional tiny scroll-back (human behavior)
            if random.random() < 0.1 and progress > 0.15:
                progress -= random.uniform(0.02, 0.05)
            await page.evaluate(f"window.scrollTo(0, document.documentElement.scrollHeight * {progress})")
            # Variable delays — humans don't scroll at fixed intervals
            delay = base_delay * random.uniform(0.5, 1.8)
            # Occasional longer pause (reading something)
            if random.random() < 0.15:
                delay += random.uniform(0.8, 2.0)
            await asyncio.sleep(delay)
        # Final scroll to absolute bottom
        await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        await asyncio.sleep(base_delay * random.uniform(1.0, 2.5))

    async def _trigger_players(self, page):
        """Force HLS.js to initialize and request the M3U8 manifest."""
        try:
            await page.evaluate("""
                // Step 1: Hover all clip/video containers to trigger lazy init
                document.querySelectorAll(
                    'video, [class*="clip"], [class*="video"], [class*="preview"], [class*="player"]'
                ).forEach(el => {
                    ['mouseenter','mouseover','pointermove','focus'].forEach(evt =>
                        el.dispatchEvent(new MouseEvent(evt, {bubbles:true, cancelable:true}))
                    );
                });

                // Step 2: Force every <video> to load + play
                // HLS.js attaches to <video> and requests the M3U8 when play() is called.
                document.querySelectorAll('video').forEach(v => {
                    try {
                        // Remove muted restriction so autoplay works
                        v.muted = true;
                        v.preload = 'auto';
                        if (v.readyState === 0) v.load();
                        v.play().catch(() => {});
                    } catch(e) {}
                });

                // Step 3: Scroll to any video element to trigger IntersectionObserver-based lazy loaders
                const firstVideo = document.querySelector('video');
                if (firstVideo) firstVideo.scrollIntoView({block:'center', behavior:'instant'});
            """)
        except Exception as e:
            self.log(f"Player trigger error: {str(e)[:80]}", "DEBUG")

    async def _collect_js_m3u8s(self, page, source_url, clip_meta):
        """Read any video URLs captured by our XHR/fetch/DOM interceptor script."""
        try:
            urls = await page.evaluate("window.__interceptedVideoUrls__ || []")
            current_id = clip_meta.get('clip_id', '')
            recorded = 0
            skipped = 0
            for u in urls:
                if u and isinstance(u, str):
                    u = u.strip()
                    if not self._video_re.search(u):
                        continue
                    # Filter: only record URLs matching current clip's video ID
                    if current_id:
                        vid_m = re.search(r'/video-files/(\d+)/', u)
                        if vid_m and vid_m.group(1) != current_id:
                            skipped += 1
                            continue
                    await self._record_video_url(u, source_url, clip_meta)
                    recorded += 1
            if recorded or skipped:
                self.log(
                    f"  [js-intercept] {recorded} recorded, {skipped} skipped (other clips' previews)",
                    "DEBUG")
        except Exception as e:
            self.log(f"JS intercept collection error: {str(e)[:80]}", "DEBUG")

    async def _extract_links(self, page):
        try:
            return await page.evaluate("""
                [...document.querySelectorAll('a[href]')]
                    .map(a=>a.href).filter(h=>h&&h.startsWith('http'))
            """)
        except Exception: return []

    def _normalize_url(self, url):
        return self.profile.normalize_url(url)

    def _is_excluded(self, url): return self.profile.is_excluded(url)
    def _is_catalog(self, url):  return self.profile.is_catalog(url)

    def _is_clip(self, url):
        """Check if URL is an individual item page using the active profile."""
        return self.profile.is_item(url)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN WINDOW
# ─────────────────────────────────────────────────────────────────────────────

# (field_key, header_label, default_width)
# ─────────────────────────────────────────────────────────────────────────────
# FLOW LAYOUT  — wrapping grid for card view
# ─────────────────────────────────────────────────────────────────────────────

class FlowLayout(QLayout):
    """Standard wrapping flow layout — places items left-to-right, wraps on overflow."""
    def __init__(self, parent=None, h_spacing=8, v_spacing=8):
        super().__init__(parent)
        self._items    = []
        self._h_gap    = h_spacing
        self._v_gap    = v_spacing

    def addItem(self, item):        self._items.append(item)
    def count(self):                return len(self._items)
    def itemAt(self, i):            return self._items[i] if 0 <= i < len(self._items) else None
    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):  return Qt.Orientation(0)
    def hasHeightForWidth(self):    return True
    def heightForWidth(self, w):    return self._do_layout(QRect(0,0,w,0), test=True)
    def setGeometry(self, rect):    super().setGeometry(rect); self._do_layout(rect, test=False)
    def sizeHint(self):             return self.minimumSize()

    def minimumSize(self):
        sz = QSize()
        for item in self._items:
            sz = sz.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return sz + QSize(m.left()+m.right(), m.top()+m.bottom())

    def _do_layout(self, rect, test=False):
        m = self.contentsMargins()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        right = rect.right() - m.right()
        row_h = 0
        for item in self._items:
            w = item.widget()
            if w and w.isHidden():
                continue
            sh = item.sizeHint()
            if x + sh.width() > right and row_h > 0:
                x = rect.x() + m.left()
                y += row_h + self._v_gap
                row_h = 0
            if not test:
                item.setGeometry(QRect(QPoint(x, y), sh))
            x += sh.width() + self._h_gap
            row_h = max(row_h, sh.height())
        return y + row_h - rect.y() + m.bottom()


# ─────────────────────────────────────────────────────────────────────────────
# STAR RATING WIDGET
# ─────────────────────────────────────────────────────────────────────────────

class StarRating(QWidget):
    """Clickable 5-star rating widget."""
    rating_changed = pyqtSignal(int)

    def __init__(self, rating=0, size=20, interactive=True, parent=None):
        super().__init__(parent)
        self._rating = rating
        self._hover_rating = 0
        self._star_size = size
        self._interactive = interactive
        self.setFixedHeight(size + 4)
        self.setFixedWidth(size * 5 + 8)
        if interactive:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setMouseTracking(True)

    def set_rating(self, r):
        self._rating = max(0, min(5, r))
        self.update()

    def rating(self):
        return self._rating

    def mouseMoveEvent(self, e):
        if self._interactive:
            self._hover_rating = self._star_at(e.position().x())
            self.update()

    def leaveEvent(self, e):
        self._hover_rating = 0
        self.update()

    def mousePressEvent(self, e):
        if self._interactive:
            new_r = self._star_at(e.position().x())
            self._rating = 0 if new_r == self._rating else new_r
            self.rating_changed.emit(self._rating)
            self.update()

    def _star_at(self, x):
        return max(1, min(5, int(x / self._star_size) + 1))

    def paintEvent(self, e):
        from math import cos, sin, pi
        from PyQt6.QtGui import QPolygon
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        display = self._hover_rating if self._hover_rating > 0 else self._rating
        s = self._star_size
        for i in range(5):
            if i < display:
                p.setPen(QColor('#f9e2af'))
                p.setBrush(QBrush(QColor('#f9e2af')))
            else:
                p.setPen(QColor('#45475a'))
                p.setBrush(QBrush(QColor('#313244')))
            cx = i * s + s // 2
            cy = s // 2 + 2
            # Draw a simple star shape
            pts = []
            for j in range(10):
                angle = pi / 2 + j * pi / 5
                r = s * 0.42 if j % 2 == 0 else s * 0.18
                pts.append(QPoint(int(cx + r * cos(angle)), int(cy - r * sin(angle))))
            p.drawPolygon(QPolygon(pts))
        p.end()


# ─────────────────────────────────────────────────────────────────────────────
# CLIP CARD  — visual card for the grid view
# ─────────────────────────────────────────────────────────────────────────────

class ClipCard(QFrame):
    tag_clicked = pyqtSignal(str)
    hover_enter = pyqtSignal(object, object)  # (row_data, card_widget)
    hover_leave = pyqtSignal(object)           # (card_widget,)

    # (card_width, thumb_height)  for size-index 0=S 1=M 2=L
    SIZES = [(160, 90), (200, 112), (240, 135)]

    @staticmethod
    def _get_field(row, key):
        """Safely extract a field from a sqlite3.Row or dict."""
        try:
            keys = row.keys() if hasattr(row, 'keys') else []
            return str(row[key] if key in keys and row[key] else '')
        except (KeyError, IndexError, TypeError):
            return ''

    def __init__(self, row, size_idx=1, thumb_dir=''):
        super().__init__()
        _g = lambda k: self._get_field(row, k)

        self._row       = row        # keep full row for hover/click
        self._clip_id   = _g('clip_id')
        self._m3u8      = _g('m3u8_url')
        self._local     = _g('local_path')
        self._dl_status = _g('dl_status')
        self._favorited = int(_g('favorited') or 0)
        self._user_rating = int(_g('user_rating') or 0)
        self._size_idx  = size_idx
        self._hover_player = None      # QMediaPlayer for hover preview
        self._hover_video  = None      # QVideoWidget overlay
        self._hover_audio  = None

        cw, th = self.SIZES[size_idx]
        self.setFixedWidth(cw)
        self.setObjectName('clip-card')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(0, 0, 0, 8)
        vlay.setSpacing(0)

        # ── Thumbnail ─────────────────────────────────────────────────────
        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(cw, th)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setStyleSheet(
            f"background:#11111b; border-radius:7px 7px 0 0; border:none;")
        self._cw, self._th = cw, th
        self._set_placeholder()
        vlay.addWidget(self.thumb_label)

        # ── Content ───────────────────────────────────────────────────────
        content = QWidget()
        content.setStyleSheet("background:transparent;")
        clay = QVBoxLayout(content)
        clay.setContentsMargins(8, 5, 8, 0)
        clay.setSpacing(2)

        # Title (2 lines max)
        title = _g('title') or self._clip_id
        self.title_lbl = QLabel(title)
        self.title_lbl.setWordWrap(True)
        self.title_lbl.setMaximumHeight(34)
        self.title_lbl.setStyleSheet(
            "color:#cdd6f4; font-size:11px; font-weight:700; background:transparent;")
        self.title_lbl.setToolTip(title)
        clay.addWidget(self.title_lbl)

        # Creator
        creator = _g('creator')
        if creator:
            cl = QLabel(creator)
            cl.setStyleSheet(
                "color:#f9e2af; font-size:10px; background:transparent;")
            cl.setToolTip(creator)
            clay.addWidget(cl)

        # Badges row: resolution | duration | fps | fav heart | rating | status dot
        badges = QHBoxLayout(); badges.setSpacing(3); badges.setContentsMargins(0,3,0,0)
        # Favorite heart
        if self._favorited:
            heart = QLabel('\u2665')
            heart.setStyleSheet("color:#f38ba8; font-size:10px; background:transparent;")
            heart.setToolTip("Favorited")
            badges.addWidget(heart)
        for txt, clr in [(_g('resolution'),'#cba6f7'),(_g('duration'),'#89b4fa'),(_g('frame_rate'),'#a6e3a1')]:
            if txt:
                b = QLabel(txt)
                b.setStyleSheet(
                    f"background:{clr}22; color:{clr}; font-size:8px; "
                    f"font-weight:700; padding:1px 5px; border-radius:3px;")
                badges.addWidget(b)
        badges.addStretch()
        # Rating stars (compact)
        if self._user_rating > 0:
            stars_text = '\u2605' * self._user_rating
            stars = QLabel(stars_text)
            stars.setStyleSheet("color:#f9e2af; font-size:8px; background:transparent;")
            badges.addWidget(stars)
        ds = self._dl_status
        dot_clr = {'done':'#a6e3a1','downloading':'#f9e2af','error':'#f38ba8'}.get(ds,'#313244')
        dot = QLabel('●')
        dot.setStyleSheet(f"color:{dot_clr}; font-size:10px; background:transparent;")
        dot.setToolTip({'done':'Downloaded','downloading':'Downloading...','error':'Error','':''}.get(ds,''))
        badges.addWidget(dot)
        clay.addLayout(badges)

        # Tag chips (first 5 tags)
        tags_raw = _g('tags')
        tags = [t.strip() for t in tags_raw.split(',') if t.strip()][:5]
        if tags:
            trow = QHBoxLayout(); trow.setSpacing(3); trow.setContentsMargins(0,3,0,0)
            for t in tags:
                tb = QPushButton(t[:18])
                tb.setObjectName('tag-chip')
                tb.setFixedHeight(16)
                tb.clicked.connect(lambda _=False, tag=t: self.tag_clicked.emit(tag))
                trow.addWidget(tb)
            trow.addStretch()
            clay.addLayout(trow)

        vlay.addWidget(content)

        # Load thumb if available
        tp = _g('thumb_path') if 'thumb_path' in keys else ''
        if tp and os.path.isfile(tp):
            self.set_thumb(tp)
        elif thumb_dir and self._clip_id:
            candidate = os.path.join(thumb_dir, f"{self._clip_id}.jpg")
            if os.path.isfile(candidate):
                self.set_thumb(candidate)

    def _set_placeholder(self):
        pm = QPixmap(self._cw, self._th)
        pm.fill(QColor('#11111b'))
        # Draw a subtle film-frame icon
        painter = QPainter(pm)
        painter.setPen(QColor('#313244'))
        painter.drawRect(self._cw//2-16, self._th//2-12, 32, 24)
        painter.setPen(QColor('#45475a'))
        painter.drawText(QRect(0, 0, self._cw, self._th),
                         Qt.AlignmentFlag.AlignCenter, '▶')
        painter.end()
        self.thumb_label.setPixmap(pm)

    def set_thumb(self, path):
        try:
            pm = QPixmap(path)
            if pm.isNull(): return
            # Scale to fit, letterbox with dark background
            pm = pm.scaled(self._cw, self._th,
                           Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
            canvas = QPixmap(self._cw, self._th)
            canvas.fill(QColor('#11111b'))
            painter = QPainter(canvas)
            x = (self._cw - pm.width()) // 2
            y = (self._th - pm.height()) // 2
            painter.drawPixmap(x, y, pm)
            painter.end()
            self.thumb_label.setPixmap(canvas)
        except Exception as e:
            print(f"[UI] Thumb render error for {getattr(self, '_clip_id', '?')}: {e}")

    def update_dl_status(self, status):
        self._dl_status = status

    # ── Hover video preview ───────────────────────────────────────────────

    def enterEvent(self, event):
        """Start video preview on hover if local file exists."""
        super().enterEvent(event)
        if not _HAS_VIDEO: return
        local = self._local
        if not local or not os.path.isfile(local): return
        try:
            if not self._hover_video:
                self._hover_video = QVideoWidget(self.thumb_label)
                self._hover_video.setGeometry(0, 0, self._cw, self._th)
                self._hover_video.setStyleSheet("background:#11111b; border-radius:7px 7px 0 0;")
                self._hover_audio = QAudioOutput()
                self._hover_audio.setVolume(0.0)  # muted on hover
                self._hover_player = QMediaPlayer()
                self._hover_player.setAudioOutput(self._hover_audio)
                self._hover_player.setVideoOutput(self._hover_video)
            self._hover_player.setSource(QUrl.fromLocalFile(local))
            self._hover_video.show()
            self._hover_video.raise_()
            self._hover_player.play()
        except Exception as e:
            print(f"[UI] Hover preview error: {e}")

    def leaveEvent(self, event):
        """Stop hover preview."""
        super().leaveEvent(event)
        if self._hover_player:
            self._hover_player.stop()
        if self._hover_video:
            self._hover_video.hide()

    def cleanup_hover(self):
        """Call before destroying card to release media resources."""
        if self._hover_player:
            self._hover_player.stop()
            self._hover_player.setSource(QUrl())
            self._hover_player = None
        if self._hover_video:
            self._hover_video.setParent(None)
            self._hover_video.deleteLater()
            self._hover_video = None
        self._hover_audio = None


# ─────────────────────────────────────────────────────────────────────────────
# THUMBNAIL WORKER  — background extractor / fetcher
# ─────────────────────────────────────────────────────────────────────────────

class ThumbnailWorker(QThread):
    """
    Extracts/fetches thumbnails for clips in background.
    Priority: local MP4 → thumbnail_url → (M3U8 on demand).
    """
    thumb_ready = pyqtSignal(str, str)   # clip_id, thumb_path
    all_done    = pyqtSignal()

    def __init__(self, clips, thumb_dir, db):
        super().__init__()
        self.clips     = clips
        self.thumb_dir = thumb_dir
        self.db        = db
        self._stop     = threading.Event()

    def stop(self): self._stop.set()

    @staticmethod
    def _get_field(row, key):
        """Safely extract a field from a sqlite3.Row or dict."""
        try:
            keys = row.keys() if hasattr(row, 'keys') else []
            return str(row[key] if key in keys and row[key] else '')
        except (KeyError, IndexError, TypeError):
            return ''

    def run(self):
        os.makedirs(self.thumb_dir, exist_ok=True)
        ffmpeg = _get_ffmpeg()

        for clip in self.clips:
            if self._stop.is_set():
                break

            clip_id    = self._get_field(clip, 'clip_id')
            local_path = self._get_field(clip, 'local_path')
            thumb_url  = self._get_field(clip, 'thumbnail_url')
            m3u8_url   = self._get_field(clip, 'm3u8_url')

            if not clip_id:
                continue

            out_path = os.path.join(self.thumb_dir, f"{clip_id}.jpg")

            # Already on disk — update DB and notify
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                self.db.update_thumb_path(clip_id, out_path)
                self.thumb_ready.emit(clip_id, out_path)
                continue

            ok = False

            # 1. Extract from downloaded MP4 (fastest, highest quality)
            if not ok and local_path and os.path.isfile(local_path):
                ok = self._from_mp4(ffmpeg, local_path, out_path)

            # 2. Fetch Artlist's own thumbnail URL
            if not ok and thumb_url:
                ok = self._from_url(thumb_url, out_path)

            if ok:
                self.db.update_thumb_path(clip_id, out_path)
                self.thumb_ready.emit(clip_id, out_path)

        self.all_done.emit()

    def _from_mp4(self, ffmpeg, mp4_path, out_path):
        try:
            cmd = [ffmpeg, '-y',
                   '-ss', '3',
                   '-i', mp4_path,
                   '-frames:v', '1',
                   '-vf', 'thumbnail,scale=320:-1',
                   '-q:v', '3',
                   out_path]
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            return r.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0
        except Exception:
            return False

    def _from_url(self, url, out_path):
        try:
            import urllib.request as _ur
            req = _ur.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with _ur.urlopen(req, timeout=15) as resp:
                with open(out_path, 'wb') as f:
                    f.write(resp.read())
            return os.path.isfile(out_path) and os.path.getsize(out_path) > 0
        except Exception:
            return False



# Column definitions — retained for reference and export
COLUMNS = [
    ('favorited',  '\u2665',          30),
    ('user_rating','Rating',       60),
    ('title',      'Title',       260),
    ('creator',    'Creator',     130),
    ('collection', 'Collection',  140),
    ('tags',       'Tags',        220),
    ('resolution', 'Res',          60),
    ('duration',   'Dur',          50),
    ('frame_rate', 'FPS',          40),
    ('camera',     'Camera',      110),
    ('formats',    'Formats',      80),
    ('dl_status',  'Status',       70),
    ('m3u8_url',   'M3U8 URL',    180),
    ('clip_id',    'Clip ID',      70),
]

# Used by download tab list items to store local file path
_LOCAL_PATH_ROLE = Qt.ItemDataRole.UserRole



# ─────────────────────────────────────────────────────────────────────────────
# DOWNLOAD WORKER
# ─────────────────────────────────────────────────────────────────────────────

_ffmpeg_cache = None

def _get_ffmpeg():
    """Return path to ffmpeg executable, using imageio-ffmpeg bundled binary. Cached after first lookup."""
    global _ffmpeg_cache
    if _ffmpeg_cache is not None:
        return _ffmpeg_cache
    try:
        import imageio_ffmpeg
        _ffmpeg_cache = imageio_ffmpeg.get_ffmpeg_exe()
        return _ffmpeg_cache
    except Exception:
        pass  # imageio_ffmpeg not installed — fall through to PATH check
    # Fallback: check PATH
    import shutil
    _ffmpeg_cache = shutil.which('ffmpeg') or 'ffmpeg'
    return _ffmpeg_cache


def _apply_fn_template(template, clip, clip_id, ext='.mp4'):
    """Build filename from a user template with {title}, {clip_id}, {creator}, etc."""
    def _g(k): return str(clip.get(k, '') or '').strip()
    title_clean = re.sub(r'[<>:"/\\|?*]', '', _g('title'))[:60].rstrip('_.')
    sample = {
        'title':      title_clean or f'clip_{clip_id or "unknown"}',
        'clip_id':    clip_id or 'unknown',
        'creator':    re.sub(r'[<>:"/\\|?*]', '', _g('creator'))[:40] or 'unknown',
        'collection': re.sub(r'[<>:"/\\|?*]', '', _g('collection'))[:40] or 'unknown',
        'resolution': _g('resolution') or '',
    }
    try:
        result = template.format(**sample)
    except Exception:
        result = f"{sample['title']}_{clip_id}"
    # ALWAYS append clip_id if not already present to prevent filename collisions
    if clip_id and clip_id not in result:
        result = f"{result}_{clip_id}"
    result = result.replace('/', os.sep)
    parts = result.split(os.sep)
    clean_parts = [re.sub(r'[<>:"/\\|?*]', '', p).strip().rstrip('_.') or 'clip' for p in parts]
    result = os.sep.join(clean_parts)
    result = re.sub(r'\s+', '_', result)
    return result + ext


def _safe_filename(title, clip_id, ext='.mp4'):
    """Generate a safe filesystem filename from title + clip_id."""
    safe = re.sub(r'[<>:"/\\|?*]', '', title or '').strip()
    safe = re.sub(r'\s+', '_', safe)[:60].rstrip('_.')
    base = f"{safe}_{clip_id}" if safe else f"clip_{clip_id}"
    return base + ext


class DownloadWorker(QThread):
    """
    Persistent download queue worker with concurrent downloads, retry with
    exponential backoff, and real-time speed + ETA tracking.
    """
    progress_signal = pyqtSignal(str, int, str)   # clip_id, percent, status_text
    clip_done       = pyqtSignal(str, bool, str)  # clip_id, success, local_path_or_error
    log_signal      = pyqtSignal(str, str)         # message, level
    speed_signal    = pyqtSignal(str, float)        # clip_id, bytes_per_sec
    all_done        = pyqtSignal()

    def __init__(self, out_dir, db, max_concurrent=2, max_retries=3):
        super().__init__()
        import queue as _queue
        self.out_dir        = out_dir
        self.db             = db
        self._queue         = _queue.Queue()
        self._stop          = threading.Event()
        self._procs         = {}               # clip_id -> subprocess.Popen
        self._procs_lock    = threading.Lock()
        self._seen          = set()
        self._fn_template   = (load_config() or {}).get('fn_template', '{title}')
        self.max_concurrent = max_concurrent
        self.max_retries    = max_retries
        self._active_count  = 0
        self._active_lock   = threading.Lock()
        # Pre-populate _seen with already-downloaded clips to prevent re-downloads
        try:
            rows = db.execute(
                "SELECT clip_id FROM clips WHERE dl_status='done' AND local_path != ''").fetchall()
            for r in rows:
                if r['clip_id']:
                    self._seen.add(r['clip_id'])
            if self._seen:
                self.log_signal.emit(
                    f"Download worker: {len(self._seen)} clips already downloaded (skipping)",
                    "INFO")
        except Exception:
            pass

    def enqueue(self, clip):
        """Thread-safe: add a clip dict/Row to the download queue. Returns True if actually queued."""
        cid = clip['clip_id'] if hasattr(clip, '__getitem__') else clip.get('clip_id', '')
        if not cid or cid in self._seen:
            return False
        m3u8 = clip['m3u8_url'] if hasattr(clip, '__getitem__') else clip.get('m3u8_url', '')
        if not m3u8:
            return False
        self._seen.add(cid)
        self._queue.put(dict(clip))
        self.log_signal.emit(
            f"[DL-Q] Enqueued id:{cid} ({self._queue.qsize()} in queue)", "INFO")
        return True

    def queue_size(self):
        return self._queue.qsize()

    def stop(self):
        self._stop.set()
        # Push a single sentinel to unblock the main dispatch loop's queue.get()
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        with self._procs_lock:
            for proc in self._procs.values():
                try: proc.terminate()
                except Exception: pass

    def log(self, msg, level='INFO'):
        self.log_signal.emit(msg, level)

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import queue as _queue

        os.makedirs(self.out_dir, exist_ok=True)
        ffmpeg = _get_ffmpeg()
        self.log(f"Download worker ready  |  ffmpeg: {ffmpeg}  |  concurrent: {self.max_concurrent}", "INFO")

        with ThreadPoolExecutor(max_workers=self.max_concurrent) as pool:
            futures = {}
            while not self._stop.is_set():
                # Submit new jobs up to max_concurrent
                while len(futures) < self.max_concurrent and not self._stop.is_set():
                    try:
                        clip = self._queue.get(timeout=0.5)
                    except _queue.Empty:
                        break
                    if clip is None or self._stop.is_set():
                        break  # Sentinel or stop — exit submit loop
                    fut = pool.submit(self._download_one_with_retry, clip, ffmpeg)
                    futures[fut] = clip

                # Check for completed futures
                done_futs = [f for f in futures if f.done()]
                for f in done_futs:
                    del futures[f]
                    try:
                        f.result()
                    except Exception as e:
                        self.log(f"Unhandled download error: {e}", "ERROR")

                if not futures and self._queue.empty():
                    # Brief wait for new items before declaring idle
                    import time as _time
                    _time.sleep(1.0)
                    if self._queue.empty() and not futures and not self._stop.is_set():
                        break

            # Cancel remaining futures on stop
            if self._stop.is_set():
                for f in list(futures):
                    f.cancel()
            # Wait for remaining futures
            for f in futures:
                try: f.result(timeout=30)
                except Exception: pass

        self.all_done.emit()

    def _download_one_with_retry(self, clip, ffmpeg):
        """Download a single clip with exponential backoff retry."""
        import time as _time
        clip_id = str(clip.get('clip_id', '') or '')
        last_err = ""
        for attempt in range(self.max_retries + 1):
            if self._stop.is_set():
                return
            if attempt > 0:
                wait = min(2 ** attempt, 30)
                self.log(f"Retry {attempt}/{self.max_retries} for [{clip_id}] in {wait}s", "WARN")
                self.progress_signal.emit(clip_id, 0, f"Retry {attempt} in {wait}s...")
                _time.sleep(wait)
                if self._stop.is_set():
                    return

            success = self._download_one(clip, ffmpeg)
            if success:
                return
            last_err = f"attempt {attempt+1} failed"

        # All retries exhausted
        self.db.set_dl_status(clip_id, 'error')
        self.progress_signal.emit(clip_id, 0, f"Failed after {self.max_retries+1} attempts")
        self.log(f"Gave up [{clip_id}] after {self.max_retries+1} attempts", "ERROR")
        self.clip_done.emit(clip_id, False, f"Failed after {self.max_retries+1} attempts")

    def _download_one(self, clip, ffmpeg):
        """Download a single clip. Returns True on success, False on failure."""
        import time as _time
        clip_id      = str(clip.get('clip_id', '') or '')
        m3u8_url     = str(clip.get('m3u8_url','') or '')

        # ── Check if already downloaded ───────────────────────────────
        if clip_id:
            try:
                check = self.db.execute(
                    "SELECT dl_status, local_path, m3u8_url FROM clips WHERE clip_id=?",
                    (clip_id,)).fetchone()
                if check:
                    if check['dl_status'] == 'done' and check['local_path'] and os.path.isfile(check['local_path']):
                        self.log(f"[DL] SKIP id:{clip_id} — already downloaded: {check['local_path']}", "INFO")
                        self.progress_signal.emit(clip_id, 100, "Already downloaded")
                        self.clip_done.emit(clip_id, True, check['local_path'])
                        return True
                    # Use latest m3u8_url from DB (may have been upgraded to HD/UHD)
                    if check['m3u8_url']:
                        m3u8_url = check['m3u8_url']
            except Exception:
                pass

        # Poll DB until title is populated (metadata extraction runs after M3U8 fires).
        fresh = None
        if clip_id:
            for _attempt in range(30):          # 30 x 0.5s = 15s max wait
                if self._stop.is_set():
                    return False
                try:
                    fresh = self.db.execute(
                        "SELECT * FROM clips WHERE clip_id=?", (clip_id,)).fetchone()
                except Exception:
                    break
                if fresh and fresh['title']:
                    break
                _time.sleep(0.5)

        title        = str((fresh and fresh['title'])   or clip.get('title','')   or '')
        m3u8_url     = str((fresh and fresh['m3u8_url'])or m3u8_url               or '')
        duration_str = str((fresh and fresh['duration'])or clip.get('duration','')or '')

        if not m3u8_url:
            self.log(f"[DL] SKIP id:{clip_id} — no video URL", "WARN")
            return False

        total_secs = self._parse_duration(duration_str)
        fn_tpl    = self._fn_template
        source = fresh if fresh else clip
        clip_data  = dict(zip(source.keys(), tuple(source))) if (hasattr(source,'keys') and not isinstance(source, dict)) else (source if isinstance(source, dict) else {})
        filename   = _apply_fn_template(fn_tpl, clip_data, clip_id)
        out_path   = os.path.join(self.out_dir, filename)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        # Check if output file already exists and is non-empty
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 1024:
            self.log(f"[DL] SKIP id:{clip_id} — file already exists: {filename}", "INFO")
            self.db.update_local_path(clip_id, out_path, 'done')
            self.progress_signal.emit(clip_id, 100, "File exists")
            self.clip_done.emit(clip_id, True, out_path)
            return True

        # Determine quality label for logging
        qual = '?'
        qual_m = re.search(r'-(uhd|hd|sd)_', m3u8_url, re.IGNORECASE)
        if qual_m:
            qual = qual_m.group(1).upper()
        res_m = re.search(r'(\d{3,4})_(\d{3,4})_', m3u8_url)
        if res_m:
            qual = f"{max(int(res_m.group(1)),int(res_m.group(2)))}p"

        self.log(
            f"[DL] START id:{clip_id} [{qual}] '{title[:40]}'\n"
            f"     URL: {m3u8_url[:100]}{'...' if len(m3u8_url) > 100 else ''}\n"
            f"     -> {filename}",
            "INFO")
        self.progress_signal.emit(clip_id, 0, "Downloading...")
        self.db.set_dl_status(clip_id, 'downloading')

        try:
            cmd = [
                ffmpeg, '-y',
                '-protocol_whitelist', 'file,http,https,tcp,tls,crypto,hls',
                '-i', m3u8_url,
                '-c:v', 'copy',
                '-an',
                '-movflags', '+faststart',
                out_path
            ]

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace')

            with self._procs_lock:
                self._procs[clip_id] = proc

            ffmpeg_duration = total_secs
            dl_start_time = _time.time()
            last_speed_update = dl_start_time

            for line in proc.stderr:
                if self._stop.is_set():
                    proc.terminate(); break
                line = line.strip()
                if not ffmpeg_duration:
                    dm = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.?\d*)', line)
                    if dm:
                        h,m,s = int(dm.group(1)), int(dm.group(2)), float(dm.group(3))
                        ffmpeg_duration = h*3600 + m*60 + s
                tm = re.search(r'time=(\d+):(\d+):(\d+\.?\d*)', line)
                if tm and ffmpeg_duration:
                    h,m,s = int(tm.group(1)), int(tm.group(2)), float(tm.group(3))
                    elapsed = h*3600 + m*60 + s
                    pct = min(99, int(elapsed / ffmpeg_duration * 100))

                    # Calculate speed + ETA
                    wall_elapsed = _time.time() - dl_start_time
                    speed_str = ""
                    eta_str = ""
                    if wall_elapsed > 1 and pct > 0:
                        remaining_pct = 100 - pct
                        eta_secs = (wall_elapsed / pct) * remaining_pct
                        if eta_secs < 60:
                            eta_str = f"{eta_secs:.0f}s left"
                        else:
                            eta_str = f"{eta_secs/60:.1f}m left"
                        # Estimate speed from file size if available
                        if os.path.exists(out_path):
                            now = _time.time()
                            if now - last_speed_update >= 1.0:
                                fsize = os.path.getsize(out_path)
                                speed_bps = fsize / wall_elapsed if wall_elapsed > 0 else 0
                                if speed_bps > 1_000_000:
                                    speed_str = f"{speed_bps/1_000_000:.1f} MB/s"
                                elif speed_bps > 1000:
                                    speed_str = f"{speed_bps/1000:.0f} KB/s"
                                last_speed_update = now

                    parts = [f"{pct}%"]
                    if speed_str: parts.append(speed_str)
                    if eta_str: parts.append(eta_str)
                    status = "  |  ".join(parts)
                    self.progress_signal.emit(clip_id, pct, status)

            proc.wait()
            rc = proc.returncode

            with self._procs_lock:
                self._procs.pop(clip_id, None)

            if self._stop.is_set():
                return False

            if rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                # Report final size + speed
                fsize = os.path.getsize(out_path)
                wall_total = _time.time() - dl_start_time
                if fsize > 1_000_000:
                    size_str = f"{fsize/1_000_000:.1f} MB"
                else:
                    size_str = f"{fsize/1000:.0f} KB"
                if wall_total > 0:
                    avg_speed = fsize / wall_total
                    if avg_speed > 1_000_000:
                        speed_final = f"{avg_speed/1_000_000:.1f} MB/s"
                    else:
                        speed_final = f"{avg_speed/1000:.0f} KB/s"
                else:
                    speed_final = ""

                self._write_sidecar(clip_data, out_path)
                self.db.update_local_path(clip_id, out_path, 'done')
                self._extract_thumb(clip_id, out_path)
                done_text = f"Done  |  {size_str}"
                if speed_final: done_text += f"  |  avg {speed_final}"
                self.progress_signal.emit(clip_id, 100, done_text)
                self.log(f"Done: {filename}  ({size_str}, {speed_final})", "OK")
                self.clip_done.emit(clip_id, True, out_path)
                return True
            else:
                err = f"ffmpeg exit {rc}"
                self.progress_signal.emit(clip_id, 0, f"Error (exit {rc})")
                self.log(f"Failed [{clip_id}]: {err}", "ERROR")
                return False

        except Exception as e:
            with self._procs_lock:
                self._procs.pop(clip_id, None)
            err = str(e)
            self.progress_signal.emit(clip_id, 0, f"Error: {err[:60]}")
            self.log(f"Download error [{clip_id}]: {err}", "ERROR")
            return False

    def _parse_duration(self, s):
        """Parse 'MM:SS' or 'HH:MM:SS' to seconds."""
        if not s: return 0.0
        try:
            parts = [float(x) for x in s.strip().split(':')]
            if len(parts) == 2:   return parts[0]*60 + parts[1]
            if len(parts) == 3:   return parts[0]*3600 + parts[1]*60 + parts[2]
        except Exception: pass
        return 0.0

    def _write_sidecar(self, clip, mp4_path):
        """Write a .json sidecar file next to the MP4 with full clip metadata."""
        sidecar = os.path.splitext(mp4_path)[0] + '.json'
        def _g(k): return str(clip.get(k, '') or '')
        data = {
            'clip_id':      _g('clip_id'),
            'title':        _g('title'),
            'creator':      _g('creator'),
            'collection':   _g('collection'),
            'tags':         _g('tags'),
            'resolution':   _g('resolution'),
            'duration':     _g('duration'),
            'frame_rate':   _g('frame_rate'),
            'camera':       _g('camera'),
            'formats':      _g('formats'),
            'm3u8_url':     _g('m3u8_url'),
            'source_url':   _g('source_url'),
            'source_site':  _g('source_site'),
            'local_path':   mp4_path,
            'downloaded_at': datetime.now().isoformat(),
        }
        try:
            with open(sidecar, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[DL WARN] Sidecar write failed for {clip_data.get('clip_id','?')}: {e}")


    def _extract_thumb(self, clip_id, mp4_path):
        """Extract a thumbnail from a downloaded MP4 and store in DB."""
        try:
            thumb_dir = os.path.join(os.path.dirname(mp4_path), '..', 'thumbs')
            thumb_dir = os.path.normpath(thumb_dir)
            os.makedirs(thumb_dir, exist_ok=True)
            out = os.path.join(thumb_dir, f"{clip_id}.jpg")
            if os.path.isfile(out) and os.path.getsize(out) > 0:
                self.db.update_thumb_path(clip_id, out)
                return
            ffmpeg = _get_ffmpeg()
            cmd = [ffmpeg, '-y', '-ss', '3', '-i', mp4_path,
                   '-frames:v', '1', '-vf', 'thumbnail,scale=320:-1',
                   '-q:v', '3', out]
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
                self.db.update_thumb_path(clip_id, out)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# TOAST NOTIFICATION OVERLAY
# ─────────────────────────────────────────────────────────────────────────────

class ToastNotification(QLabel):
    """Non-blocking overlay toast that auto-fades. Call MainWindow._toast()."""
    def __init__(self, parent, message, level='info', duration=3000):
        super().__init__(message, parent)
        colors = {
            'info':    ('#89b4fa', '#1e1e2e'),
            'success': ('#a6e3a1', '#1e1e2e'),
            'warning': ('#f9e2af', '#1e1e2e'),
            'error':   ('#f38ba8', '#1e1e2e'),
        }
        fg, bg = colors.get(level, colors['info'])
        self.setStyleSheet(
            f"background:{bg}; color:{fg}; border:1px solid {fg}; "
            f"border-radius:6px; padding:10px 20px; font-size:12px; font-weight:600;")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.adjustSize()
        self.setFixedWidth(max(self.width() + 40, 280))
        # Position: top-center of parent
        px = (parent.width() - self.width()) // 2
        self.move(px, 60)
        self.show()
        self.raise_()
        # Fade out after duration
        self._fade_timer = QTimer(self)
        self._fade_timer.setSingleShot(True)
        self._fade_timer.timeout.connect(self._start_fade)
        self._fade_timer.start(duration)

    def _start_fade(self):
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)
        self._anim = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._anim.setDuration(400)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self.deleteLater)
        self._anim.start()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.db              = None
        self.worker          = None
        self._dl_worker      = None   # DownloadWorker instance
        self._db_path        = os.path.join(get_config_dir(), 'artlist_results.db')

        self.setWindowTitle("Video Scraper  v0.7.1")
        self.setMinimumSize(960, 600)
        self.resize(1400, 860)

        self._init_db()
        self._build_ui()
        self._load_saved_config()
        self._check_browser_status()

        self._dl_clip_rows = {}   # populated by _start_downloads
        self._dl_done_count = 0
        self._last_rows = []
        self._thumb_worker = None
        self._load_more_btn = None
        self._bg_workers = []     # prevent GC of background QThread workers
        self._active_profile = SiteProfile.get('Artlist')

        self._do_search()
        self._update_stats()
        self._refresh_filter_dropdowns()
        self._refresh_collections_combo()
        self._refresh_saved_searches()

        self._stats_timer = QTimer()
        self._stats_timer.timeout.connect(self._update_stats)
        self._stats_timer.start(5000)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self._dl_stats_timer = QTimer()
        self._dl_stats_timer.timeout.connect(self._update_dl_stats)
        self._dl_stats_timer.start(5000)

        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._do_search)

        # Debounce timer for clip_signal — batches UI refresh during active crawl
        self._clip_found_timer = QTimer()
        self._clip_found_timer.setSingleShot(True)
        self._clip_found_timer.timeout.connect(lambda: (self._do_search(), self._update_dl_stats()))

        # ── Keyboard Shortcuts ──────────────────────────────────────────────
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_search)
        QShortcut(QKeySequence("F5"), self, activated=self._do_search)
        QShortcut(QKeySequence("Ctrl+1"), self, activated=lambda: self.tabs.setCurrentIndex(0))
        QShortcut(QKeySequence("Ctrl+2"), self, activated=lambda: self.tabs.setCurrentIndex(1))
        QShortcut(QKeySequence("Ctrl+3"), self, activated=lambda: self.tabs.setCurrentIndex(2))
        QShortcut(QKeySequence("Ctrl+4"), self, activated=lambda: self.tabs.setCurrentIndex(3))
        QShortcut(QKeySequence("Ctrl+5"), self, activated=lambda: self.tabs.setCurrentIndex(4))
        QShortcut(QKeySequence("Ctrl+6"), self, activated=lambda: self.tabs.setCurrentIndex(5))

        # ── System Tray ─────────────────────────────────────────────────────
        self._setup_tray()

        # ── Clipboard Monitor (opt-in — enable in config) ────────────────────
        self._clipboard_timer = QTimer()
        self._clipboard_timer.timeout.connect(self._check_clipboard)
        self._last_clipboard = ""
        # Only start if previously enabled in config
        cfg = load_config()
        if cfg.get('clipboard_monitor', False):
            self._clipboard_timer.start(2000)

    def _init_db(self):
        self.db = DB(self._db_path)

    # ── System Tray ────────────────────────────────────────────────────────

    def _setup_tray(self):
        """Create system tray icon with context menu."""
        self._tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        # Build a simple colored icon
        pm = QPixmap(32, 32)
        pm.fill(QColor('#89b4fa'))
        p = QPainter(pm)
        p.setPen(QColor('#1e1e2e'))
        p.setFont(QFont('Segoe UI', 16, QFont.Weight.Bold))
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, 'A')
        p.end()
        icon = QIcon(pm)
        self.setWindowIcon(icon)

        self._tray = QSystemTrayIcon(icon, self)
        tray_menu = QMenu()
        show_action = QAction("Show Window", self)
        show_action.triggered.connect(self._tray_show)
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._tray_quit)
        tray_menu.addAction(quit_action)
        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._tray_show()

    def _tray_show(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _tray_quit(self):
        self._tray_quitting = True
        self.close()

    def changeEvent(self, event):
        """Minimize to tray instead of taskbar."""
        super().changeEvent(event)
        if (event.type() == event.Type.WindowStateChange and
                self.isMinimized() and self._tray and self._tray.isVisible()):
            QTimer.singleShot(0, self.hide)
            self._tray.showMessage(
                "Artlist Scraper", "Running in background. Double-click tray to restore.",
                QSystemTrayIcon.MessageIcon.Information, 2000)

    # ── Toast Notifications ─────────────────────────────────────────────────

    def _toast(self, message, level='info', duration=3000):
        """Show a non-blocking overlay toast notification."""
        ToastNotification(self, message, level, duration)

    # ── Clipboard Monitor ───────────────────────────────────────────────────

    def _check_clipboard(self):
        """Auto-detect video site URLs copied to clipboard."""
        try:
            text = QApplication.clipboard().text().strip()
            if text == self._last_clipboard or not text:
                return
            self._last_clipboard = text
            if not text.startswith('http'):
                return
            # Check against active profile domains (or accept all if Generic)
            profile = getattr(self, '_active_profile', None) or SiteProfile.get('Artlist')
            domain = urlparse(text).netloc
            if profile.is_allowed_domain(domain):
                self._toast(f"URL detected: {text[:60]}...", 'info', 4000)
                if hasattr(self, 'inp_url'):
                    self.inp_url.setText(text)
        except Exception:
            pass

    # ── Keyboard Shortcut Helpers ───────────────────────────────────────────

    def _focus_search(self):
        """Ctrl+F — switch to search tab and focus the search input."""
        self.tabs.setCurrentIndex(2)
        self.inp_search.setFocus()
        self.inp_search.selectAll()

    # ── Header ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        root.addWidget(self._build_header())

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        root.addWidget(self.tabs)

        self.tabs.addTab(self._build_config_tab(),      "⚙️  Configure")
        self.tabs.addTab(self._build_crawl_tab(),       "🔍  Crawl")
        self.tabs.addTab(self._build_search_tab(),      "🔎  Search")
        self.tabs.addTab(self._build_download_tab(),    "⬇️  Download")
        self.tabs.addTab(self._build_archive_tab(),     "📦  Archive")
        self.tabs.addTab(self._build_export_tab(),      "💾  Export")

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready  —  DB: " + self._db_path)

    def _build_header(self):
        hdr = QFrame()
        hdr.setFixedHeight(52)
        hdr.setStyleSheet("background:#181825; border-bottom:1px solid #313244;")
        lay = QHBoxLayout(hdr)
        lay.setContentsMargins(20,0,20,0)

        t = QLabel("🎬  Artlist M3U8 Scraper")
        t.setStyleSheet("font-size:15px; font-weight:700; color:#89b4fa; background:transparent;")
        lay.addWidget(t)
        lay.addStretch()

        self.lbl_clips_hdr  = self._hdr_lbl("Clips: 0",  "#cdd6f4")
        self.lbl_m3u8_hdr   = self._hdr_lbl("M3U8: 0",   "#a6e3a1")
        self.lbl_status_hdr = self._hdr_lbl("● Idle",    "#6c7086")

        for lbl in (self.lbl_clips_hdr, self.lbl_m3u8_hdr, self.lbl_status_hdr):
            lay.addSpacing(22); lay.addWidget(lbl)
        return hdr

    def _hdr_lbl(self, text, color):
        l = QLabel(text)
        l.setStyleSheet(f"font-size:12px; font-weight:600; background:transparent; color:{color};")
        return l

    # ── Config Tab ──────────────────────────────────────────────────────────

    def _build_config_tab(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget(); scroll.setWidget(inner)
        lay = QVBoxLayout(inner); lay.setContentsMargins(24,20,24,24); lay.setSpacing(14)

        # Site Profiles — multi-select for round-robin rotation
        grp_profile = QGroupBox("Site Profiles  (round-robin rotation)"); gp = QVBoxLayout(grp_profile)
        self._profile_checks = {}
        for name in SiteProfile.all_names():
            prof = SiteProfile.get(name)
            chk = QCheckBox(f"{name}  --  {prof.description[:55]}")
            chk.setChecked(name in ('Artlist', 'Pexels'))
            chk.setStyleSheet("color:#cdd6f4; font-size:12px;")
            gp.addWidget(chk)
            self._profile_checks[name] = chk
        brow = QHBoxLayout()
        brow.addWidget(QLabel("Batch size per site:"))
        self.spin_batch_size = QSpinBox(); self.spin_batch_size.setRange(5, 500)
        self.spin_batch_size.setValue(50); self.spin_batch_size.setSuffix(" pages")
        self.spin_batch_size.setToolTip("Pages to crawl per site before rotating to the next")
        brow.addWidget(self.spin_batch_size); brow.addStretch()
        gp.addLayout(brow)
        self.lbl_profile_desc = QLabel("Select one or more profiles. Crawler rotates between them.")
        self.lbl_profile_desc.setObjectName("subtext")
        self.lbl_profile_desc.setWordWrap(True)
        gp.addWidget(self.lbl_profile_desc)
        lay.addWidget(grp_profile)

        # Target URL
        grp = QGroupBox("Target"); g = QVBoxLayout(grp)
        r = QHBoxLayout(); r.addWidget(QLabel("Start URL:"))
        self.inp_url = QLineEdit("https://artlist.io/stock-footage/")
        self.inp_url.setMinimumWidth(200); r.addWidget(self.inp_url, 1); g.addLayout(r)
        g.addWidget(self._sub("Crawler follows all video links from this page automatically."))
        lay.addWidget(grp)

        # Rate limits
        grp2 = QGroupBox("Rate Limiting  (Server Safety)"); g2 = QVBoxLayout(grp2)
        def spinrow(lbl, attr, mn, mx, val, sfx, tip):
            r2 = QHBoxLayout(); l = QLabel(lbl); l.setFixedWidth(140); r2.addWidget(l)
            sp = QSpinBox(); sp.setRange(mn,mx); sp.setValue(val)
            sp.setSuffix(sfx); sp.setFixedWidth(130); setattr(self, attr, sp)
            r2.addWidget(sp); r2.addSpacing(10)
            t2 = QLabel(tip); t2.setObjectName("subtext"); r2.addWidget(t2,1); return r2
        g2.addLayout(spinrow("Page delay:",   'spin_page_delay',   500,30000,2500," ms","Wait between page loads."))
        g2.addLayout(spinrow("Scroll delay:", 'spin_scroll_delay', 100, 5000, 800, " ms","Wait between scroll steps."))
        g2.addLayout(spinrow("M3U8 wait:",    'spin_m3u8_wait',   1000,15000,4000," ms","Dwell after load for player to fire M3U8 requests."))
        g2.addLayout(spinrow("Scroll steps:", 'spin_scroll_steps',   3,   50,  15,   "","Scroll passes on catalog pages."))
        g2.addLayout(spinrow("Page timeout:", 'spin_timeout',     5000,120000,30000," ms","Max load wait before skip."))
        lay.addWidget(grp2)

        # Limits
        grp3 = QGroupBox("Crawl Limits  (0 = unlimited)"); g3 = QVBoxLayout(grp3)
        g3.addLayout(spinrow("Max pages:", 'spin_max_pages', 0,99999,0,"","Stop after N pages."))
        g3.addLayout(spinrow("Max depth:", 'spin_max_depth', 1,   10,2,"","Depth from start URL."))
        lay.addWidget(grp3)

        # Options
        grp4 = QGroupBox("Options"); g4 = QHBoxLayout(grp4)
        self.chk_headless = QCheckBox("Headless mode"); self.chk_headless.setChecked(True)
        self.chk_headless.setToolTip("Uncheck to see the browser — required for solving CAPTCHAs/challenges")
        self.chk_resume   = QCheckBox("Resume mode  (skip already-crawled pages)"); self.chk_resume.setChecked(True)
        g4.addWidget(self.chk_headless); g4.addSpacing(30); g4.addWidget(self.chk_resume); g4.addStretch()
        lay.addWidget(grp4)

        # Output dir
        grp5 = QGroupBox("Output Directory"); g5 = QHBoxLayout(grp5)
        self.inp_output = QLineEdit(os.path.join(os.path.expanduser('~'), 'ArtlistScraper', 'output'))
        g5.addWidget(self.inp_output,1)
        bb = QPushButton("Browse..."); bb.setObjectName("neutral"); bb.setFixedWidth(90)
        bb.clicked.connect(self._browse_output); g5.addWidget(bb)
        lay.addWidget(grp5)

        btns = QHBoxLayout()
        sb = QPushButton("💾  Save Config"); sb.clicked.connect(self._save_cfg); sb.setFixedHeight(36)
        lb = QPushButton("📂  Load Config"); lb.setObjectName("neutral"); lb.clicked.connect(self._load_cfg_file); lb.setFixedHeight(36)
        btns.addWidget(sb); btns.addWidget(lb); btns.addStretch()
        lay.addLayout(btns); lay.addStretch()
        return scroll

    # ── Crawl Tab ───────────────────────────────────────────────────────────

    def _build_crawl_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(20,16,20,16); lay.setSpacing(12)

        # Stat cards
        cards = QHBoxLayout()
        for attr, label, color in [
            ('stat_clips',  'Clips Found',  '#89b4fa'),
            ('stat_m3u8',   'With M3U8',    '#a6e3a1'),
            ('stat_pages',  'Pages Done',   '#cba6f7'),
            ('stat_queued', 'In Queue',     '#f9e2af'),
            ('stat_errors', 'Errors',       '#f38ba8'),
        ]:
            card = QFrame(); card.setObjectName('stat-card'); card.setFixedHeight(76)
            cl = QVBoxLayout(card); cl.setContentsMargins(14,8,14,8); cl.setSpacing(2)
            lv = QLabel("0")
            lv.setStyleSheet(f"font-size:24px; font-weight:700; color:{color}; background:transparent;")
            lv.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ll = QLabel(label); ll.setStyleSheet("color:#6c7086; font-size:11px; background:transparent;")
            ll.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cl.addWidget(lv); cl.addWidget(ll); setattr(self, attr, lv); cards.addWidget(card)
        lay.addLayout(cards)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0,0); self.progress_bar.setFixedHeight(5)
        self.progress_bar.setVisible(False); lay.addWidget(self.progress_bar)

        # ── Browser status banner ──────────────────────────────────────────
        self.browser_banner = QFrame()
        self.browser_banner.setStyleSheet(
            "background:#2a1a1a; border:1px solid #f38ba8; border-radius:6px; padding:6px;")
        bb_lay = QHBoxLayout(self.browser_banner)
        bb_lay.setContentsMargins(12,6,12,6)
        self.lbl_browser_status = QLabel("⚠  Chromium browser not found — click Install to set it up.")
        self.lbl_browser_status.setStyleSheet("color:#f38ba8; font-weight:600;")
        bb_lay.addWidget(self.lbl_browser_status, 1)
        self.btn_install_browser = QPushButton("Install Browser")
        self.btn_install_browser.setObjectName("danger")
        self.btn_install_browser.setFixedHeight(30); self.btn_install_browser.setFixedWidth(140)
        self.btn_install_browser.clicked.connect(self._install_browser)
        bb_lay.addWidget(self.btn_install_browser)
        lay.addWidget(self.browser_banner)
        btns = QHBoxLayout()
        self.btn_start = QPushButton("▶  Start Crawl")
        self.btn_start.setObjectName("success"); self.btn_start.setFixedHeight(40)
        self.btn_start.clicked.connect(self._start_crawl)

        self.btn_pause = QPushButton("⏸  Pause")
        self.btn_pause.setObjectName("warning"); self.btn_pause.setFixedHeight(40)
        self.btn_pause.setEnabled(False); self.btn_pause.clicked.connect(self._toggle_pause)

        self.btn_stop = QPushButton("⏹  Stop")
        self.btn_stop.setObjectName("danger"); self.btn_stop.setFixedHeight(40)
        self.btn_stop.setEnabled(False); self.btn_stop.clicked.connect(self._stop_crawl)

        clrdb  = QPushButton("🗑  Clear DB");   clrdb.setObjectName("neutral");  clrdb.setFixedHeight(40);  clrdb.clicked.connect(self._clear_db)
        rebuild_fts = QPushButton("🔄  Rebuild Index"); rebuild_fts.setObjectName("neutral"); rebuild_fts.setFixedHeight(40); rebuild_fts.setToolTip("Rebuild full-text search index if search results seem wrong"); rebuild_fts.clicked.connect(self._rebuild_fts)
        clrlog = QPushButton("Clear Log"); clrlog.setObjectName("neutral"); clrlog.setFixedHeight(40); clrlog.clicked.connect(lambda: self.log_view.clear())
        self.chk_verbose_log = QCheckBox("Verbose")
        self.chk_verbose_log.setChecked(True)
        self.chk_verbose_log.setToolTip("Show DEBUG-level log messages (detailed troubleshooting output)")
        self.chk_verbose_log.setStyleSheet("color:#6c7086; font-size:11px;")

        for b in (self.btn_start, self.btn_pause, self.btn_stop): btns.addWidget(b)
        btns.addStretch(); btns.addWidget(self.chk_verbose_log); btns.addWidget(rebuild_fts); btns.addWidget(clrdb); btns.addWidget(clrlog)
        lay.addLayout(btns)

        log_lbl = QLabel("Live Log"); log_lbl.setStyleSheet("color:#6c7086; font-size:11px; font-weight:600;")
        lay.addWidget(log_lbl)
        self.log_view = QTextEdit(); self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        lay.addWidget(self.log_view, 1)
        return w

    # ── Search Tab ──────────────────────────────────────────────────────────

    def _build_search_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(20,16,20,12); lay.setSpacing(8)

        # ── Search bar + view controls ────────────────────────────────────
        srow = QHBoxLayout(); srow.setSpacing(6)
        self.inp_search = QLineEdit()
        self.inp_search.setPlaceholderText(
            "Search by title, tags, creator, collection, camera, resolution...")
        self.inp_search.setMinimumHeight(38)
        self.inp_search.returnPressed.connect(lambda: (self._search_timer.stop(), self._do_search()))
        self.inp_search.textChanged.connect(lambda: self._search_timer.start(350))
        srow.addWidget(self.inp_search, 1)

        sb = QPushButton("Search"); sb.setFixedHeight(38); sb.setFixedWidth(80)
        sb.clicked.connect(self._do_search); srow.addWidget(sb)
        lay.addLayout(srow)

        # ── Filter row 1: column filters ──────────────────────────────────
        frow = QHBoxLayout(); frow.setSpacing(4); frow.setContentsMargins(0,0,0,0)
        filter_defs = [
            ('combo_source',     'Source',     'source_site'),
            ('combo_creator',    'Creator',    'creator'),
            ('combo_collection', 'Collection', 'collection'),
            ('combo_res',        'Res',        'resolution'),
            ('combo_fps',        'FPS',        'frame_rate'),
        ]
        self._filter_map = {}
        for attr, label, col in filter_defs:
            lbl = QLabel(label+":")
            lbl.setStyleSheet("color:#6c7086; font-size:11px;")
            frow.addWidget(lbl)
            cb = QComboBox(); cb.setMinimumWidth(70); cb.setMaximumWidth(180)
            cb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            cb.addItem("All"); setattr(self, attr, cb)
            self._filter_map[attr] = col
            cb.currentTextChanged.connect(self._do_search)
            frow.addWidget(cb)

        rfbtn = QPushButton("↻"); rfbtn.setObjectName("neutral")
        rfbtn.setFixedSize(28, 28); rfbtn.setToolTip("Refresh filters")
        rfbtn.clicked.connect(self._refresh_filter_dropdowns); frow.addWidget(rfbtn)
        frow.addStretch()
        lay.addLayout(frow)

        # ── Filter row 2: asset management — scrollable to prevent cutoff ─
        frow2_scroll = QScrollArea()
        frow2_scroll.setWidgetResizable(True)
        frow2_scroll.setFrameShape(QFrame.Shape.NoFrame)
        frow2_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        frow2_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        frow2_scroll.setFixedHeight(44)
        frow2_scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")

        frow2_w = QWidget(); frow2_w.setStyleSheet("background:transparent;")
        frow2 = QHBoxLayout(frow2_w); frow2.setSpacing(6); frow2.setContentsMargins(0,2,0,2)

        # Duration range
        lbl_d = QLabel("Dur:"); lbl_d.setStyleSheet("color:#6c7086; font-size:11px;")
        frow2.addWidget(lbl_d)
        self.combo_duration = QComboBox(); self.combo_duration.setMinimumWidth(65)
        for d in ['All', '0-10s', '10-30s', '30s-1m', '1-5m', '5m+']:
            self.combo_duration.addItem(d)
        self.combo_duration.currentTextChanged.connect(self._do_search)
        frow2.addWidget(self.combo_duration)

        # Collection filter
        lbl_c = QLabel("Coll:"); lbl_c.setStyleSheet("color:#6c7086; font-size:11px;")
        frow2.addWidget(lbl_c)
        self.combo_user_collection = QComboBox(); self.combo_user_collection.setMinimumWidth(90)
        self.combo_user_collection.addItem("All")
        self.combo_user_collection.currentTextChanged.connect(self._do_search)
        frow2.addWidget(self.combo_user_collection)

        # Vertical separator
        sep1 = QFrame(); sep1.setFrameShape(QFrame.Shape.VLine)
        sep1.setStyleSheet("color:#313244;"); sep1.setFixedWidth(1)
        frow2.addWidget(sep1)

        # Favorites toggle
        self.chk_favorites = QCheckBox("\u2665 Fav")
        self.chk_favorites.setStyleSheet("color:#f38ba8; font-size:11px; font-weight:600;")
        self.chk_favorites.toggled.connect(self._do_search)
        frow2.addWidget(self.chk_favorites)

        # Downloaded toggle
        self.chk_downloaded = QCheckBox("\u2713 DL'd")
        self.chk_downloaded.setStyleSheet("color:#a6e3a1; font-size:11px; font-weight:600;")
        self.chk_downloaded.toggled.connect(self._do_search)
        frow2.addWidget(self.chk_downloaded)

        # AND/OR toggle
        self.btn_search_mode = QPushButton("OR")
        self.btn_search_mode.setObjectName("neutral"); self.btn_search_mode.setFixedSize(36, 26)
        self.btn_search_mode.setCheckable(True); self.btn_search_mode.setToolTip("Toggle AND/OR search mode")
        self.btn_search_mode.clicked.connect(self._toggle_search_mode)
        frow2.addWidget(self.btn_search_mode)

        # Min rating filter
        lbl_mr = QLabel("\u2605:"); lbl_mr.setStyleSheet("color:#f9e2af; font-size:11px;")
        frow2.addWidget(lbl_mr)
        self.spin_min_rating = QSpinBox(); self.spin_min_rating.setRange(0, 5)
        self.spin_min_rating.setValue(0); self.spin_min_rating.setFixedWidth(44)
        self.spin_min_rating.setToolTip("Minimum star rating")
        self.spin_min_rating.valueChanged.connect(self._do_search)
        frow2.addWidget(self.spin_min_rating)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setStyleSheet("color:#313244;"); sep2.setFixedWidth(1)
        frow2.addWidget(sep2)

        # Saved searches
        self.combo_saved_search = QComboBox(); self.combo_saved_search.setMinimumWidth(120)
        self.combo_saved_search.addItem("Saved Searches...")
        self.combo_saved_search.activated.connect(self._load_saved_search)
        frow2.addWidget(self.combo_saved_search)

        btn_save_search = QPushButton("Save"); btn_save_search.setObjectName("neutral")
        btn_save_search.setFixedSize(44, 26); btn_save_search.setToolTip("Save current search as preset")
        btn_save_search.clicked.connect(self._save_current_search)
        frow2.addWidget(btn_save_search)

        sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.VLine)
        sep3.setStyleSheet("color:#313244;"); sep3.setFixedWidth(1)
        frow2.addWidget(sep3)

        # Card size slider (only visible in card mode)
        self.lbl_card_size = QLabel("Size:")
        self.lbl_card_size.setStyleSheet("color:#6c7086; font-size:11px;")
        self.card_size_slider = QSlider(Qt.Orientation.Horizontal)
        self.card_size_slider.setRange(0, 2); self.card_size_slider.setValue(1)
        self.card_size_slider.setFixedWidth(70)
        self.card_size_slider.valueChanged.connect(self._on_card_size_changed)
        frow2.addWidget(self.lbl_card_size); frow2.addWidget(self.card_size_slider)

        clrbtn = QPushButton("Clear"); clrbtn.setObjectName("neutral"); clrbtn.setFixedSize(48, 26)
        clrbtn.clicked.connect(self._clear_search); frow2.addWidget(clrbtn)

        frow2_scroll.setWidget(frow2_w)
        lay.addWidget(frow2_scroll)

        # ── Main area: results splitter (left=cards, right=detail panel) ──
        self._search_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._search_splitter.setHandleWidth(4)
        self._search_splitter.setStyleSheet("QSplitter::handle { background:#313244; }")
        lay.addWidget(self._search_splitter, 1)

        # ── Card grid (only view) ────────────────────────────────────────
        self._card_scroll = QScrollArea()
        self._card_scroll.setWidgetResizable(True)
        self._card_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._card_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._card_scroll.setStyleSheet("QScrollArea { background:#1e1e2e; }")
        self._card_container = QWidget()
        self._card_container.setStyleSheet("background:#1e1e2e;")
        self._card_flow = FlowLayout(self._card_container, h_spacing=10, v_spacing=10)
        self._card_flow.setContentsMargins(12, 12, 12, 12)
        self._card_container.setLayout(self._card_flow)
        self._card_scroll.setWidget(self._card_container)
        self._search_splitter.addWidget(self._card_scroll)

        self._current_cards = []   # list of ClipCard widgets
        self._selected_card = None  # currently selected card

        # ── Detail panel (right side, always visible) ─────────────────────
        self._detail_panel = self._build_detail_panel()
        self._search_splitter.addWidget(self._detail_panel)
        self._detail_panel.setVisible(True)
        self._search_splitter.setSizes([700, 360])
        self._search_splitter.setCollapsible(1, False)

        # ── Bottom row ────────────────────────────────────────────────────
        brow = QHBoxLayout()
        self.lbl_result_count = QLabel("0 results"); self.lbl_result_count.setObjectName("subtext")
        brow.addWidget(self.lbl_result_count); brow.addStretch()
        self.btn_fetch_thumbs = QPushButton("Fetch Thumbnails")
        self.btn_fetch_thumbs.setObjectName("neutral"); self.btn_fetch_thumbs.setFixedHeight(30)
        self.btn_fetch_thumbs.clicked.connect(self._start_thumb_worker)
        brow.addWidget(self.btn_fetch_thumbs)
        lay.addLayout(brow)
        return w

    def _on_card_size_changed(self, val):
        self._populate_cards(self._last_rows if hasattr(self, '_last_rows') else [])

    def _on_tag_clicked(self, tag):
        self.inp_search.setText(tag)
        self._do_search()
        self.status_bar.showMessage(f"Filtering by tag: {tag}", 3000)

    # ── Detail Panel ────────────────────────────────────────────────────────

    def _build_detail_panel(self):
        """Right-side detail panel — asset management hub with preview, rating, notes, tags, collections."""
        panel = QFrame()
        panel.setStyleSheet("QFrame { background:#181825; border-left:1px solid #313244; }")
        panel.setMinimumWidth(320); panel.setMaximumWidth(440)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(14,12,14,12); lay.setSpacing(8)

        # ── Preview area (video player or thumbnail) ──────────────────────
        self._preview_stack = QStackedWidget()
        self._preview_stack.setFixedHeight(200)

        # Page 0: static thumbnail
        self.detail_thumb = QLabel()
        self.detail_thumb.setFixedHeight(200)
        self.detail_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_thumb.setStyleSheet("background:#11111b; border-radius:6px; border:none;")
        self._preview_stack.addWidget(self.detail_thumb)  # index 0

        # Page 1: video player (if available)
        self._video_player = None
        self._video_widget = None
        self._audio_output = None
        if _HAS_VIDEO:
            self._video_widget = QVideoWidget()
            self._video_widget.setFixedHeight(200)
            self._video_widget.setStyleSheet("background:#11111b; border-radius:6px;")
            self._audio_output = QAudioOutput()
            self._audio_output.setVolume(0.5)
            self._video_player = QMediaPlayer()
            self._video_player.setAudioOutput(self._audio_output)
            self._video_player.setVideoOutput(self._video_widget)
            self._preview_stack.addWidget(self._video_widget)  # index 1
        lay.addWidget(self._preview_stack)

        # ── Video controls (play/pause/stop + scrub) ──────────────────────
        if _HAS_VIDEO:
            vctrl = QHBoxLayout(); vctrl.setSpacing(4)
            self.btn_preview_play = QPushButton("Play")
            self.btn_preview_play.setObjectName("success"); self.btn_preview_play.setFixedHeight(28)
            self.btn_preview_play.setFixedWidth(60)
            self.btn_preview_play.clicked.connect(self._preview_toggle_play)
            vctrl.addWidget(self.btn_preview_play)
            self.btn_preview_stop = QPushButton("Stop")
            self.btn_preview_stop.setObjectName("neutral"); self.btn_preview_stop.setFixedHeight(28)
            self.btn_preview_stop.setFixedWidth(50)
            self.btn_preview_stop.clicked.connect(self._preview_stop)
            vctrl.addWidget(self.btn_preview_stop)
            self.preview_scrub = QSlider(Qt.Orientation.Horizontal)
            self.preview_scrub.setRange(0, 1000); self.preview_scrub.setFixedHeight(20)
            self.preview_scrub.sliderMoved.connect(self._preview_seek)
            vctrl.addWidget(self.preview_scrub, 1)
            self.lbl_preview_time = QLabel("0:00")
            self.lbl_preview_time.setStyleSheet("color:#6c7086; font-size:10px; font-family:Consolas;")
            vctrl.addWidget(self.lbl_preview_time)
            lay.addLayout(vctrl)
            # Timer for scrub updates
            self._preview_timer = QTimer()
            self._preview_timer.timeout.connect(self._preview_update_scrub)
            self._preview_timer.start(250)

        # ── Title + favorite ──────────────────────────────────────────────
        title_row = QHBoxLayout(); title_row.setSpacing(6)
        self.detail_title = QLabel("Select a clip")
        self.detail_title.setWordWrap(True)
        self.detail_title.setStyleSheet("color:#cdd6f4; font-size:13px; font-weight:700;")
        title_row.addWidget(self.detail_title, 1)
        self.btn_detail_fav = QPushButton("\u2661")
        self.btn_detail_fav.setFixedSize(32, 32)
        self.btn_detail_fav.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_detail_fav.setStyleSheet(
            "font-size:18px; background:transparent; border:none; color:#45475a;")
        self.btn_detail_fav.setToolTip("Toggle favorite")
        self.btn_detail_fav.clicked.connect(self._detail_toggle_fav)
        title_row.addWidget(self.btn_detail_fav)
        lay.addLayout(title_row)

        # ── Star rating ───────────────────────────────────────────────────
        self.detail_stars = StarRating(0, size=18, interactive=True)
        self.detail_stars.rating_changed.connect(self._detail_set_rating)
        lay.addWidget(self.detail_stars)

        # ── Scrollable metadata + notes + tags area ───────────────────────
        meta_scroll = QScrollArea(); meta_scroll.setWidgetResizable(True)
        meta_scroll.setFrameShape(QFrame.Shape.NoFrame)
        meta_scroll.setStyleSheet("QScrollArea { background:transparent; }")
        meta_inner = QWidget(); meta_inner.setStyleSheet("background:transparent;")
        self._detail_meta_lay = QVBoxLayout(meta_inner)
        self._detail_meta_lay.setContentsMargins(0,0,0,0); self._detail_meta_lay.setSpacing(4)
        meta_scroll.setWidget(meta_inner)
        lay.addWidget(meta_scroll, 1)

        # ── User notes ────────────────────────────────────────────────────
        notes_lbl = QLabel("Notes"); notes_lbl.setStyleSheet("color:#6c7086; font-size:10px; font-weight:600;")
        lay.addWidget(notes_lbl)
        self.detail_notes = QTextEdit()
        self.detail_notes.setMaximumHeight(60)
        self.detail_notes.setPlaceholderText("Add notes about this clip...")
        self.detail_notes.setStyleSheet(
            "background:#11111b; color:#cdd6f4; border:1px solid #313244; "
            "border-radius:4px; font-size:11px; padding:4px;")
        self._notes_save_timer = QTimer(); self._notes_save_timer.setSingleShot(True)
        self._notes_save_timer.timeout.connect(self._detail_save_notes)
        self.detail_notes.textChanged.connect(lambda: self._notes_save_timer.start(800))
        lay.addWidget(self.detail_notes)

        # ── User tags ─────────────────────────────────────────────────────
        tags_lbl = QLabel("My Tags (comma-separated)")
        tags_lbl.setStyleSheet("color:#6c7086; font-size:10px; font-weight:600;")
        lay.addWidget(tags_lbl)
        self.detail_user_tags = QLineEdit()
        self.detail_user_tags.setPlaceholderText("e.g. hero-shot, b-roll, client-xyz")
        self.detail_user_tags.setFixedHeight(28)
        self.detail_user_tags.setStyleSheet(
            "background:#11111b; color:#89b4fa; border:1px solid #313244; "
            "border-radius:4px; font-size:11px; padding:2px 6px;")
        self._tags_save_timer = QTimer(); self._tags_save_timer.setSingleShot(True)
        self._tags_save_timer.timeout.connect(self._detail_save_user_tags)
        self.detail_user_tags.textChanged.connect(lambda: self._tags_save_timer.start(800))
        lay.addWidget(self.detail_user_tags)

        # ── Collection management ─────────────────────────────────────────
        coll_row = QHBoxLayout(); coll_row.setSpacing(4)
        coll_lbl = QLabel("Collections:"); coll_lbl.setStyleSheet("color:#6c7086; font-size:10px; font-weight:600;")
        coll_row.addWidget(coll_lbl)
        self.detail_coll_combo = QComboBox(); self.detail_coll_combo.setFixedHeight(26)
        self.detail_coll_combo.setMinimumWidth(100)
        self.detail_coll_combo.addItem("Add to collection...")
        coll_row.addWidget(self.detail_coll_combo, 1)
        btn_add_coll = QPushButton("+"); btn_add_coll.setObjectName("success")
        btn_add_coll.setFixedSize(26, 26); btn_add_coll.setToolTip("Add to selected collection")
        btn_add_coll.clicked.connect(self._detail_add_to_collection)
        coll_row.addWidget(btn_add_coll)
        btn_new_coll = QPushButton("New"); btn_new_coll.setObjectName("neutral")
        btn_new_coll.setFixedSize(40, 26); btn_new_coll.setToolTip("Create new collection")
        btn_new_coll.clicked.connect(self._detail_create_collection)
        coll_row.addWidget(btn_new_coll)
        lay.addLayout(coll_row)

        # Collection chips (shows which collections this clip belongs to)
        self._detail_coll_chips = QWidget(); self._detail_coll_chips.setStyleSheet("background:transparent;")
        self._detail_coll_chips_lay = FlowLayout(self._detail_coll_chips, h_spacing=4, v_spacing=4)
        self._detail_coll_chips_lay.setContentsMargins(0,0,0,0)
        lay.addWidget(self._detail_coll_chips)

        # ── Action buttons ────────────────────────────────────────────────
        btn_row1 = QHBoxLayout()
        self.btn_detail_play = QPushButton("Open File")
        self.btn_detail_play.setObjectName("success"); self.btn_detail_play.setFixedHeight(30)
        self.btn_detail_play.clicked.connect(self._detail_play)
        btn_row1.addWidget(self.btn_detail_play)
        self.btn_detail_copy_m3u8 = QPushButton("Copy M3U8")
        self.btn_detail_copy_m3u8.setObjectName("neutral"); self.btn_detail_copy_m3u8.setFixedHeight(30)
        self.btn_detail_copy_m3u8.clicked.connect(self._detail_copy_m3u8)
        btn_row1.addWidget(self.btn_detail_copy_m3u8)
        self.btn_detail_open_folder = QPushButton("Folder")
        self.btn_detail_open_folder.setObjectName("neutral"); self.btn_detail_open_folder.setFixedHeight(30)
        self.btn_detail_open_folder.clicked.connect(self._detail_open_file)
        btn_row1.addWidget(self.btn_detail_open_folder)
        self.btn_detail_source = QPushButton("Web")
        self.btn_detail_source.setObjectName("neutral"); self.btn_detail_source.setFixedHeight(30)
        self.btn_detail_source.clicked.connect(self._detail_open_source)
        btn_row1.addWidget(self.btn_detail_source)
        lay.addLayout(btn_row1)

        self._detail_clip = None
        return panel

    def _show_detail(self, row):
        if row is None: return
        keys = row.keys() if hasattr(row, 'keys') else {}
        def _g(k): return str(row[k] if k in keys and row[k] else '')

        self._detail_clip = row
        clip_id   = _g('clip_id')
        title     = _g('title') or clip_id
        thumb_p   = _g('thumb_path')
        local_p   = _g('local_path')
        m3u8      = _g('m3u8_url')
        source    = _g('source_url')
        thumb_dir = self._thumb_dir()

        self.detail_title.setText(title)

        # ── Favorite state ────────────────────────────────────────────────
        fav = int(_g('favorited') or 0)
        self.btn_detail_fav.setText('\u2665' if fav else '\u2661')
        self.btn_detail_fav.setStyleSheet(
            f"font-size:18px; background:transparent; border:none; "
            f"color:{'#f38ba8' if fav else '#45475a'};")

        # ── Star rating ───────────────────────────────────────────────────
        self.detail_stars.set_rating(int(_g('user_rating') or 0))

        # ── Preview: video player or thumbnail ────────────────────────────
        has_local = bool(local_p and os.path.isfile(local_p))
        if _HAS_VIDEO and has_local and self._video_player:
            self._video_player.setSource(QUrl.fromLocalFile(local_p))
            self._preview_stack.setCurrentIndex(1)
            # Auto-play video on selection
            self._video_player.play()
        else:
            # Stop any playing video
            if self._video_player:
                self._video_player.stop()
            self._preview_stack.setCurrentIndex(0)
            # Show thumbnail
            pm = None
            if thumb_p and os.path.isfile(thumb_p): pm = QPixmap(thumb_p)
            elif clip_id:
                cand = os.path.join(thumb_dir, f"{clip_id}.jpg")
                if os.path.isfile(cand): pm = QPixmap(cand)
            if pm and not pm.isNull():
                scaled = pm.scaled(408, 200, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
                canvas = QPixmap(408, 200); canvas.fill(QColor('#11111b'))
                painter = QPainter(canvas)
                painter.drawPixmap((408-scaled.width())//2, (200-scaled.height())//2, scaled)
                painter.end()
                self.detail_thumb.setPixmap(canvas)
            else:
                self.detail_thumb.setText("No thumbnail")
                self.detail_thumb.setStyleSheet(
                    "background:#11111b; border-radius:6px; color:#45475a; font-size:13px;")

        # ── Metadata rows ─────────────────────────────────────────────────
        while self._detail_meta_lay.count():
            item = self._detail_meta_lay.takeAt(0)
            if item.widget(): item.widget().deleteLater()

        meta_fields = [
            ('Creator',    _g('creator'),    '#f9e2af'),
            ('Collection', _g('collection'), '#a6e3a1'),
            ('Resolution', _g('resolution'), '#cba6f7'),
            ('Duration',   _g('duration'),   '#89b4fa'),
            ('FPS',        _g('frame_rate'), '#89b4fa'),
            ('Camera',     _g('camera'),     '#cdd6f4'),
            ('Formats',    _g('formats'),    '#cdd6f4'),
            ('Status',     ('\u2713 Downloaded' if has_local else ('\u2717 Error' if _g('dl_status')=='error' else '\u2014')),
                          '#a6e3a1' if has_local else ('#f38ba8' if _g('dl_status')=='error' else '#45475a')),
            ('Clip ID',    clip_id,          '#6c7086'),
        ]
        for label, val, color in meta_fields:
            if not val: continue
            row_w = QWidget(); row_w.setStyleSheet("background:transparent;")
            row_h = QHBoxLayout(row_w); row_h.setContentsMargins(0,0,0,0); row_h.setSpacing(8)
            lbl_k = QLabel(label+":"); lbl_k.setStyleSheet("color:#6c7086; font-size:10px; font-weight:600;"); lbl_k.setFixedWidth(72)
            lbl_v = QLabel(val); lbl_v.setStyleSheet(f"color:{color}; font-size:10px;"); lbl_v.setWordWrap(True)
            row_h.addWidget(lbl_k); row_h.addWidget(lbl_v, 1)
            self._detail_meta_lay.addWidget(row_w)

        # Artlist tags (clickable)
        tags_raw = _g('tags')
        tags = [t.strip() for t in tags_raw.split(',') if t.strip()]
        if tags:
            tag_sep = QLabel("Tags"); tag_sep.setStyleSheet("color:#6c7086; font-size:10px; font-weight:600; margin-top:4px;")
            self._detail_meta_lay.addWidget(tag_sep)
            chips_w = QWidget(); chips_w.setStyleSheet("background:transparent;")
            chips_lay = FlowLayout(chips_w, h_spacing=4, v_spacing=4)
            chips_lay.setContentsMargins(0,0,0,0)
            for t in tags:
                chip = QPushButton(t); chip.setObjectName('tag-chip'); chip.setFixedHeight(18)
                chip.clicked.connect(lambda _, tag=t: self._on_tag_clicked(tag))
                chips_lay.addWidget(chip)
            chips_w.setLayout(chips_lay)
            self._detail_meta_lay.addWidget(chips_w)

        if m3u8:
            url_lbl = QLabel("M3U8:"); url_lbl.setStyleSheet("color:#6c7086; font-size:10px; font-weight:600; margin-top:4px;")
            self._detail_meta_lay.addWidget(url_lbl)
            url_val = QLabel(m3u8[:60]+("..." if len(m3u8)>60 else ""))
            url_val.setStyleSheet("color:#89b4fa; font-size:9px; font-family:Consolas,monospace;")
            url_val.setCursor(Qt.CursorShape.PointingHandCursor); url_val.setToolTip(m3u8)
            url_val.mousePressEvent = lambda e, u=m3u8: (QApplication.clipboard().setText(u),
                                                          self._toast("M3U8 copied", 'success', 1500))
            self._detail_meta_lay.addWidget(url_val)

        self._detail_meta_lay.addStretch()

        # ── Notes + user tags (populate without triggering save) ──────────
        self.detail_notes.blockSignals(True)
        self.detail_notes.setPlainText(_g('user_notes'))
        self.detail_notes.blockSignals(False)

        self.detail_user_tags.blockSignals(True)
        self.detail_user_tags.setText(_g('user_tags'))
        self.detail_user_tags.blockSignals(False)

        # ── Collection chips ──────────────────────────────────────────────
        self._refresh_detail_collections(clip_id)

        # ── Collection dropdown ───────────────────────────────────────────
        self.detail_coll_combo.blockSignals(True)
        self.detail_coll_combo.clear()
        self.detail_coll_combo.addItem("Add to collection...")
        try:
            for c in self.db.get_collections():
                self.detail_coll_combo.addItem(c['name'])
        except Exception: pass
        self.detail_coll_combo.blockSignals(False)

        # Button states
        has_m3u8  = bool(m3u8)
        self.btn_detail_play.setEnabled(has_local or has_m3u8)
        self.btn_detail_copy_m3u8.setEnabled(has_m3u8)
        self.btn_detail_open_folder.setEnabled(has_local)
        self.btn_detail_source.setEnabled(bool(source))

    def _refresh_detail_collections(self, clip_id):
        """Refresh collection chips for the currently shown clip."""
        # Clear old chips
        while self._detail_coll_chips_lay.count():
            item = self._detail_coll_chips_lay.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        try:
            for c in self.db.get_clip_collections(clip_id):
                chip = QPushButton(f"\u00d7 {c['name']}")
                chip.setFixedHeight(18)
                chip.setStyleSheet(
                    f"background:{c['color']}33; color:{c['color']}; "
                    f"font-size:9px; font-weight:700; padding:1px 6px; "
                    f"border-radius:3px; border:1px solid {c['color']}55;")
                chip.setCursor(Qt.CursorShape.PointingHandCursor)
                chip.setToolTip(f"Remove from {c['name']}")
                cid_copy = c['id']
                clip_id_copy = clip_id
                chip.clicked.connect(lambda _, ci=cid_copy, cli=clip_id_copy: self._detail_remove_from_collection(cli, ci))
                self._detail_coll_chips_lay.addWidget(chip)
        except Exception: pass

    def _on_card_clicked(self, row, card=None):
        """Show clip in detail panel and highlight the selected card."""
        # Deselect previous card
        if self._selected_card and self._selected_card is not card:
            self._selected_card.setStyleSheet("")  # revert to theme default
        # Highlight new card
        if card:
            card.setStyleSheet("QFrame#clip-card { border: 2px solid #89b4fa; background-color: #1e1e35; }")
            self._selected_card = card
        self._show_detail(row)

    def _on_card_press(self, event, row, card):
        """Handle left click (select) and right click (context menu) on cards."""
        if event.button() == Qt.MouseButton.RightButton:
            self._card_context_menu(event.globalPosition().toPoint(), row)
        else:
            self._on_card_clicked(row, card)

    def _detail_play(self):
        """Open clip in system default player."""
        if not self._detail_clip: return
        keys = self._detail_clip.keys() if hasattr(self._detail_clip, 'keys') else []
        local_p = str(self._detail_clip['local_path'] if 'local_path' in keys and self._detail_clip['local_path'] else '')
        m3u8    = str(self._detail_clip['m3u8_url']   if 'm3u8_url'   in keys and self._detail_clip['m3u8_url']   else '')
        path = local_p if (local_p and os.path.isfile(local_p)) else m3u8
        if not path: return
        try:
            if sys.platform == 'win32':    os.startfile(path)
            elif sys.platform == 'darwin': subprocess.Popen(['open', path])
            else:                          subprocess.Popen(['xdg-open', path])
        except Exception: pass

    def _detail_copy_m3u8(self):
        if not self._detail_clip: return
        keys = self._detail_clip.keys() if hasattr(self._detail_clip, 'keys') else []
        m3u8 = str(self._detail_clip['m3u8_url'] if 'm3u8_url' in keys and self._detail_clip['m3u8_url'] else '')
        if m3u8:
            QApplication.clipboard().setText(m3u8)
            self.status_bar.showMessage("M3U8 URL copied", 2000)

    def _detail_open_file(self):
        if not self._detail_clip: return
        keys = self._detail_clip.keys() if hasattr(self._detail_clip, 'keys') else []
        local_p = str(self._detail_clip['local_path'] if 'local_path' in keys and self._detail_clip['local_path'] else '')
        if local_p and os.path.isfile(local_p):
            d = os.path.dirname(local_p)
            try:
                if sys.platform == 'win32':    subprocess.Popen(['explorer', '/select,', local_p])
                elif sys.platform == 'darwin': subprocess.Popen(['open', '-R', local_p])
                else:                          subprocess.Popen(['xdg-open', d])
            except Exception: pass

    def _detail_open_source(self):
        if not self._detail_clip: return
        keys = self._detail_clip.keys() if hasattr(self._detail_clip, 'keys') else []
        source = str(self._detail_clip['source_url'] if 'source_url' in keys and self._detail_clip['source_url'] else '')
        if source:
            import webbrowser; webbrowser.open(source)

    def _card_context_menu(self, global_pos, row):
        """Right-click context menu for clip cards in grid view."""
        menu = QMenu(self)
        keys = row.keys() if hasattr(row, 'keys') else []
        def _g(k): return str(row[k] if k in keys and row[k] else '')
        cid = _g('clip_id')
        if not cid: return

        # Favorite toggle
        fav = int(_g('favorited') or 0)
        act_fav = menu.addAction("\u2665 Unfavorite" if fav else "\u2661 Favorite")
        act_fav.triggered.connect(lambda: self._ctx_toggle_favorites([cid]))

        # Rating submenu
        rating_menu = menu.addMenu("\u2605 Set Rating")
        for stars in range(6):
            label = "\u2605" * stars + "\u2606" * (5 - stars) if stars > 0 else "Clear Rating"
            act_r = rating_menu.addAction(label)
            act_r.triggered.connect(lambda _, r=stars: self._ctx_set_rating([cid], r))

        # Collection submenu
        coll_menu = menu.addMenu("Add to Collection")
        try:
            for c in self.db.get_collections():
                act_c = coll_menu.addAction(c['name'])
                cid_copy = c['id']
                act_c.triggered.connect(lambda _, ci=cid_copy: self._ctx_add_to_collection([cid], ci))
        except Exception: pass
        coll_menu.addSeparator()
        act_new_coll = coll_menu.addAction("+ New Collection...")
        act_new_coll.triggered.connect(lambda: self._ctx_new_collection([cid]))

        menu.addSeparator()

        # Download
        if _g('m3u8_url'):
            act_dl = menu.addAction("Download")
            act_dl.triggered.connect(lambda: self._start_downloads([row]))

        # Copy M3U8
        if _g('m3u8_url'):
            act_copy = menu.addAction("Copy M3U8 URL")
            act_copy.triggered.connect(lambda: (
                QApplication.clipboard().setText(_g('m3u8_url')),
                self._toast("M3U8 copied", 'success', 1500)))

        # Open in browser
        act_browser = menu.addAction("Open in Browser")
        act_browser.triggered.connect(lambda: self._ctx_open_source_urls_by_ids([cid]))

        menu.exec(global_pos)

    def _ctx_open_source_urls_by_ids(self, clip_ids):
        """Open source URLs by clip_ids."""
        import webbrowser
        for cid in clip_ids[:5]:
            try:
                row = self.db.execute("SELECT source_url FROM clips WHERE clip_id=?", (cid,)).fetchone()
                if row and row['source_url'] and row['source_url'].startswith('http'):
                    webbrowser.open(row['source_url'])
            except Exception: pass

    # ── Asset Management: Detail Panel Actions ──────────────────────────────

    def _detail_clip_id(self):
        """Get the clip_id from the currently selected detail clip."""
        if not self._detail_clip: return ''
        keys = self._detail_clip.keys() if hasattr(self._detail_clip, 'keys') else []
        return str(self._detail_clip['clip_id'] if 'clip_id' in keys and self._detail_clip['clip_id'] else '')

    def _detail_toggle_fav(self):
        cid = self._detail_clip_id()
        if not cid: return
        new_state = self.db.toggle_favorite(cid)
        self.btn_detail_fav.setText('\u2665' if new_state else '\u2661')
        self.btn_detail_fav.setStyleSheet(
            f"font-size:18px; background:transparent; border:none; "
            f"color:{'#f38ba8' if new_state else '#45475a'};")
        self._toast("Favorited" if new_state else "Unfavorited", 'success', 1500)

    def _detail_set_rating(self, rating):
        cid = self._detail_clip_id()
        if not cid: return
        self.db.set_rating(cid, rating)

    def _detail_save_notes(self):
        cid = self._detail_clip_id()
        if not cid: return
        self.db.set_notes(cid, self.detail_notes.toPlainText())

    def _detail_save_user_tags(self):
        cid = self._detail_clip_id()
        if not cid: return
        self.db.set_user_tags(cid, self.detail_user_tags.text().strip())

    def _detail_add_to_collection(self):
        cid = self._detail_clip_id()
        if not cid: return
        idx = self.detail_coll_combo.currentIndex()
        if idx <= 0: return  # "Add to collection..." placeholder
        coll_name = self.detail_coll_combo.currentText()
        try:
            coll = self.db.execute("SELECT id FROM collections WHERE name=?", (coll_name,)).fetchone()
            if coll:
                self.db.add_to_collection(cid, coll['id'])
                self._refresh_detail_collections(cid)
                self._refresh_collections_combo()
                self._toast(f"Added to {coll_name}", 'success', 1500)
        except Exception: pass
        self.detail_coll_combo.setCurrentIndex(0)

    def _detail_create_collection(self):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "New Collection", "Collection name:")
        if ok and name.strip():
            name = name.strip()
            coll_id = self.db.create_collection(name)
            if coll_id:
                # Also add current clip if one is selected
                cid = self._detail_clip_id()
                if cid:
                    self.db.add_to_collection(cid, coll_id)
                    self._refresh_detail_collections(cid)
                self._refresh_collections_combo()
                # Refresh the detail panel collection dropdown
                self.detail_coll_combo.blockSignals(True)
                self.detail_coll_combo.clear()
                self.detail_coll_combo.addItem("Add to collection...")
                for c in self.db.get_collections():
                    self.detail_coll_combo.addItem(c['name'])
                self.detail_coll_combo.blockSignals(False)
                self._toast(f"Created collection: {name}", 'success', 2000)

    def _detail_remove_from_collection(self, clip_id, collection_id):
        self.db.remove_from_collection(clip_id, collection_id)
        self._refresh_detail_collections(clip_id)
        self._refresh_collections_combo()
        self._toast("Removed from collection", 'info', 1500)

    # ── Video Preview Controls ──────────────────────────────────────────────

    def _preview_toggle_play(self):
        if not _HAS_VIDEO or not self._video_player: return
        from PyQt6.QtMultimedia import QMediaPlayer
        if self._video_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._video_player.pause()
            self.btn_preview_play.setText("Play")
        else:
            self._video_player.play()
            self.btn_preview_play.setText("Pause")

    def _preview_stop(self):
        if not _HAS_VIDEO or not self._video_player: return
        self._video_player.stop()
        self.btn_preview_play.setText("Play")
        self.preview_scrub.setValue(0)
        self.lbl_preview_time.setText("0:00")

    def _preview_seek(self, value):
        if not _HAS_VIDEO or not self._video_player: return
        dur = self._video_player.duration()
        if dur > 0:
            self._video_player.setPosition(int(dur * value / 1000))

    def _preview_update_scrub(self):
        if not _HAS_VIDEO or not self._video_player: return
        dur = self._video_player.duration()
        pos = self._video_player.position()
        if dur > 0:
            self.preview_scrub.blockSignals(True)
            self.preview_scrub.setValue(int(pos * 1000 / dur))
            self.preview_scrub.blockSignals(False)
            secs = pos // 1000
            total_secs = dur // 1000
            self.lbl_preview_time.setText(f"{secs//60}:{secs%60:02d} / {total_secs//60}:{total_secs%60:02d}")

    # ── Archive Tab ─────────────────────────────────────────────────────────

    def _build_archive_tab(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
        w = QWidget(); scroll.setWidget(w)
        lay = QVBoxLayout(w); lay.setContentsMargins(24,20,24,24); lay.setSpacing(16)

        # Stats
        grp_stats = QGroupBox("Archive Statistics"); gs = QVBoxLayout(grp_stats)
        stats_cards = QHBoxLayout()
        for attr, lbl, clr in [
            ('arc_stat_clips', 'Total Clips', '#89b4fa'),
            ('arc_stat_m3u8',  'With M3U8',  '#a6e3a1'),
            ('arc_stat_dl',    'Downloaded', '#a6e3a1'),
            ('arc_stat_errors','Errors',     '#f38ba8'),
            ('arc_stat_mb',    'Disk Used',  '#cba6f7'),
        ]:
            card = QFrame(); card.setObjectName('stat-card'); card.setFixedHeight(76)
            cl = QVBoxLayout(card); cl.setContentsMargins(14,8,14,8); cl.setSpacing(2)
            lv = QLabel("—"); lv.setStyleSheet(f"font-size:22px; font-weight:700; color:{clr}; background:transparent;")
            lv.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ll = QLabel(lbl); ll.setStyleSheet("color:#6c7086; font-size:11px; background:transparent;")
            ll.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cl.addWidget(lv); cl.addWidget(ll); setattr(self, attr, lv); stats_cards.addWidget(card)
        gs.addLayout(stats_cards)
        rfbtn = QPushButton("Refresh Stats"); rfbtn.setObjectName("neutral"); rfbtn.setFixedHeight(32); rfbtn.setFixedWidth(130)
        rfbtn.clicked.connect(self._refresh_archive_stats); gs.addWidget(rfbtn)
        lay.addWidget(grp_stats)

        # Verify
        grp_verify = QGroupBox("Verify Archive  (check local files exist on disk)")
        gv = QVBoxLayout(grp_verify)
        gv.addWidget(self._sub("Scans every downloaded clip's recorded path. Flags missing files."))
        vbtn = QPushButton("Verify Archive Integrity"); vbtn.setObjectName("warning"); vbtn.setFixedHeight(38); vbtn.setMinimumWidth(200)
        vbtn.clicked.connect(self._verify_archive); gv.addWidget(vbtn)
        self.lbl_verify_result = QLabel(""); self.lbl_verify_result.setWordWrap(True)
        self.lbl_verify_result.setStyleSheet("color:#f9e2af;"); gv.addWidget(self.lbl_verify_result)
        rbtn = QPushButton("Reset Missing to Pending"); rbtn.setObjectName("danger"); rbtn.setFixedHeight(34); rbtn.setMinimumWidth(180)
        rbtn.clicked.connect(self._reset_missing); gv.addWidget(rbtn)
        gv.addWidget(self._sub("Clears local_path + dl_status for missing files so they re-queue for download."))
        lay.addWidget(grp_verify)

        # Scan folder
        grp_scan = QGroupBox("Scan Folder  (import sidecars from disk)")
        gscan = QVBoxLayout(grp_scan)
        gscan.addWidget(self._sub("Reads .json sidecar files from a folder and imports all metadata to the database."))
        scan_row = QHBoxLayout()
        self.inp_scan_dir = QLineEdit(); self.inp_scan_dir.setPlaceholderText("Folder containing .json sidecar files...")
        scan_row.addWidget(self.inp_scan_dir, 1)
        scan_br = QPushButton("Browse..."); scan_br.setObjectName("neutral"); scan_br.setFixedWidth(90)
        scan_br.clicked.connect(self._browse_scan_dir); scan_row.addWidget(scan_br)
        gscan.addLayout(scan_row)
        scanbtn = QPushButton("Scan & Import"); scanbtn.setObjectName("success"); scanbtn.setFixedHeight(38); scanbtn.setFixedWidth(160)
        scanbtn.clicked.connect(self._scan_folder); gscan.addWidget(scanbtn)
        self.lbl_scan_result = QLabel(""); self.lbl_scan_result.setWordWrap(True)
        self.lbl_scan_result.setStyleSheet("color:#a6e3a1;"); gscan.addWidget(self.lbl_scan_result)
        lay.addWidget(grp_scan)

        # Filename template
        grp_fn = QGroupBox("Filename Template"); gfn = QVBoxLayout(grp_fn)
        gfn.addWidget(self._sub("Tokens: {title}  {clip_id}  {creator}  {collection}  {resolution}"))
        fn_row = QHBoxLayout()
        self.inp_fn_template = QLineEdit("{title}")
        fn_row.addWidget(self.inp_fn_template, 1)
        fn_save = QPushButton("Save"); fn_save.setObjectName("neutral"); fn_save.setFixedWidth(70)
        fn_save.clicked.connect(self._save_fn_template); fn_row.addWidget(fn_save)
        gfn.addLayout(fn_row)
        gfn.addWidget(self._sub("Example: {creator}/{title}_{clip_id}  creates subfolders per creator."))
        self.lbl_fn_preview = QLabel("")
        self.lbl_fn_preview.setStyleSheet("color:#89b4fa; font-size:11px; font-family:Consolas,monospace;")
        gfn.addWidget(self.lbl_fn_preview)
        self.inp_fn_template.textChanged.connect(self._update_fn_preview)
        lay.addWidget(grp_fn)

        # Retry errors
        grp_retry = QGroupBox("Retry Errors"); gr = QVBoxLayout(grp_retry)
        gr.addWidget(self._sub("Re-queue all clips that failed download."))
        retry_row = QHBoxLayout()
        self.lbl_error_count = QLabel("0 errors"); self.lbl_error_count.setStyleSheet("color:#f38ba8; font-weight:600;")
        retry_row.addWidget(self.lbl_error_count)
        rbtn2 = QPushButton("Retry All Errors"); rbtn2.setObjectName("warning"); rbtn2.setFixedHeight(36)
        rbtn2.clicked.connect(self._retry_all_errors); retry_row.addWidget(rbtn2)
        retry_row.addStretch(); gr.addLayout(retry_row)
        lay.addWidget(grp_retry)

        lay.addStretch()
        return scroll

    def _refresh_archive_stats(self):
        try:
            clips  = self.db.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
            m3u8   = self.db.execute("SELECT COUNT(*) FROM clips WHERE m3u8_url!=''").fetchone()[0]
            done   = self.db.execute("SELECT COUNT(*) FROM clips WHERE dl_status='done'").fetchone()[0]
            errors = self.db.execute("SELECT COUNT(*) FROM clips WHERE dl_status='error'").fetchone()[0]
            paths  = self.db.execute("SELECT local_path FROM clips WHERE local_path!='' AND dl_status='done'").fetchall()
            total_b = sum(os.path.getsize(r[0]) for r in paths if r[0] and os.path.isfile(r[0]))
            total_mb = total_b / 1_048_576
            mb_str = f"{total_mb:.1f} MB" if total_mb < 1024 else f"{total_mb/1024:.2f} GB"
            self.arc_stat_clips.setText(str(clips)); self.arc_stat_m3u8.setText(str(m3u8))
            self.arc_stat_dl.setText(str(done)); self.arc_stat_errors.setText(str(errors))
            self.arc_stat_mb.setText(mb_str)
            self.lbl_error_count.setText(f"{errors} error{'s' if errors!=1 else ''}")
        except Exception: pass

    def _verify_archive(self):
        try:
            rows = self.db.execute("SELECT clip_id, local_path FROM clips WHERE local_path!='' AND dl_status='done'").fetchall()
            missing = [r['clip_id'] for r in rows if not os.path.isfile(r['local_path'] or '')]
            total = len(rows)
            if not missing:
                self.lbl_verify_result.setStyleSheet("color:#a6e3a1;")
                self.lbl_verify_result.setText(f"All {total} downloaded files verified OK.")
            else:
                self.lbl_verify_result.setStyleSheet("color:#f38ba8;")
                self.lbl_verify_result.setText(
                    f"{len(missing)} of {total} files missing: "+", ".join(missing[:8])+("..." if len(missing)>8 else ""))
        except Exception as e: self.lbl_verify_result.setText(f"Error: {e}")

    def _reset_missing(self):
        try:
            rows = self.db.execute("SELECT clip_id, local_path FROM clips WHERE local_path!='' AND dl_status='done'").fetchall()
            count = 0
            for r in rows:
                if not os.path.isfile(r['local_path'] or ''):
                    self.db.execute("UPDATE clips SET local_path='', dl_status='' WHERE clip_id=?", (r['clip_id'],))
                    count += 1
            self.db.commit()
            self.lbl_verify_result.setStyleSheet("color:#f9e2af;")
            self.lbl_verify_result.setText(f"Reset {count} missing file records to pending.")
            self._do_search()
        except Exception as e: self.lbl_verify_result.setText(f"Error: {e}")

    def _browse_scan_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select Folder with Sidecar JSON Files", self._out_dir())
        if d: self.inp_scan_dir.setText(d)

    def _scan_folder(self):
        folder = self.inp_scan_dir.text().strip()
        if not folder or not os.path.isdir(folder):
            self.lbl_scan_result.setText("Choose a valid folder first."); return
        imported = 0; updated = 0; skipped = 0
        for fname in os.listdir(folder):
            if not fname.endswith('.json'): continue
            try:
                with open(os.path.join(folder, fname), encoding='utf-8') as f:
                    data = json.load(f)
                if not data.get('clip_id'): continue
                lp = data.get('local_path','')
                if lp and not os.path.isfile(lp):
                    cand = os.path.join(folder, os.path.basename(lp))
                    if os.path.isfile(cand): data['local_path'] = cand; data['dl_status'] = 'done'
                is_new = self.db.save_clip(data)
                if is_new: imported += 1
                else:
                    self.db.update_metadata(data['clip_id'], data)
                    if data.get('local_path') and os.path.isfile(data['local_path']):
                        self.db.update_local_path(data['clip_id'], data['local_path'], 'done')
                    updated += 1
            except Exception: skipped += 1
        self.lbl_scan_result.setText(f"Imported {imported} new, updated {updated}, skipped {skipped}.")
        self._do_search(); self._refresh_filter_dropdowns()

    def _save_fn_template(self):
        tpl = self.inp_fn_template.text().strip() or '{title}'
        cfg = load_config() or {}; cfg['fn_template'] = tpl; save_config(cfg)
        self.status_bar.showMessage(f"Filename template saved: {tpl}", 3000)
        self._update_fn_preview()

    def _update_fn_preview(self):
        tpl = self.inp_fn_template.text().strip() or '{title}'
        sample = {'title':'Beautiful_Sunset','clip_id':'abc123','creator':'JohnDoe','collection':'Nature','resolution':'4K'}
        try:
            preview = tpl.format(**sample)+'.mp4'
            self.lbl_fn_preview.setText(f"Preview: {preview}")
        except Exception as e: self.lbl_fn_preview.setText(f"Invalid template: {e}")

    def _retry_all_errors(self):
        rows = self.db.execute("SELECT * FROM clips WHERE dl_status='error' AND m3u8_url!=''").fetchall()
        if not rows: self.status_bar.showMessage("No errors to retry.", 3000); return
        self._ensure_dl_worker_running(); queued = 0
        for row in rows:
            data = dict(zip(row.keys(), tuple(row)))
            self.db.set_dl_status(data['clip_id'], '')
            if self._dl_worker.enqueue(data):
                self._add_dl_table_row(data); queued += 1
        self._update_overall_bar()
        self.status_bar.showMessage(f"Queued {queued} error retries.", 3000)

    # ── Download Tab ────────────────────────────────────────────────────────

    def _build_download_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(20,16,20,16); lay.setSpacing(12)

        # ── Directory row ──────────────────────────────────────────────────
        dirgrp = QGroupBox("Download Settings")
        dglay = QVBoxLayout(dirgrp)
        dlay = QHBoxLayout()
        self.inp_dl_dir = QLineEdit()
        self.inp_dl_dir.setPlaceholderText("Select folder where MP4 files will be saved...")
        dlay.addWidget(self.inp_dl_dir, 1)
        br = QPushButton("Browse..."); br.setObjectName("neutral"); br.setFixedWidth(90)
        br.clicked.connect(self._browse_dl_dir); dlay.addWidget(br)
        dglay.addLayout(dlay)
        self.chk_auto_dl = QCheckBox("Auto-download -- start downloading each clip immediately as it is scraped")
        self.chk_auto_dl.setChecked(True)
        self.chk_auto_dl.setStyleSheet("font-weight:600; color:#a6e3a1;")
        dglay.addWidget(self.chk_auto_dl)

        # ── Concurrent / retry / bandwidth settings ────────────────────────
        perf_row = QHBoxLayout(); perf_row.setSpacing(12)

        perf_row.addWidget(QLabel("Concurrent:"))
        self.spin_concurrent = QSpinBox(); self.spin_concurrent.setRange(1, 8)
        self.spin_concurrent.setValue(2); self.spin_concurrent.setFixedWidth(60)
        self.spin_concurrent.setToolTip("Number of parallel downloads")
        perf_row.addWidget(self.spin_concurrent)

        perf_row.addSpacing(10)
        perf_row.addWidget(QLabel("Max Retries:"))
        self.spin_max_retries = QSpinBox(); self.spin_max_retries.setRange(0, 10)
        self.spin_max_retries.setValue(3); self.spin_max_retries.setFixedWidth(60)
        self.spin_max_retries.setToolTip("Auto-retry failed downloads with exponential backoff")
        perf_row.addWidget(self.spin_max_retries)

        perf_row.addSpacing(10)
        perf_row.addWidget(QLabel("Speed Limit:"))
        self.spin_bw_limit = QSpinBox(); self.spin_bw_limit.setRange(0, 100000)
        self.spin_bw_limit.setValue(0); self.spin_bw_limit.setSuffix(" KB/s")
        self.spin_bw_limit.setFixedWidth(120)
        self.spin_bw_limit.setToolTip("Max download speed (0 = unlimited)")
        perf_row.addWidget(self.spin_bw_limit)
        perf_row.addStretch()

        dglay.addLayout(perf_row)
        lay.addWidget(dirgrp)

        # ── Queue stats banner ─────────────────────────────────────────────
        stats_row = QHBoxLayout()
        self.lbl_dl_queue  = self._hdr_lbl("Ready: 0",      "#89b4fa")
        self.lbl_dl_done   = self._hdr_lbl("Downloaded: 0", "#a6e3a1")
        self.lbl_dl_errors = self._hdr_lbl("Errors: 0",     "#f38ba8")
        for lb in (self.lbl_dl_queue, self.lbl_dl_done, self.lbl_dl_errors):
            stats_row.addWidget(lb); stats_row.addSpacing(24)
        stats_row.addStretch()
        lay.addLayout(stats_row)

        # Overall progress bar
        self.dl_overall_bar = QProgressBar()
        self.dl_overall_bar.setFixedHeight(8); self.dl_overall_bar.setTextVisible(False)
        self.dl_overall_bar.setVisible(False); lay.addWidget(self.dl_overall_bar)

        # Current item progress bar
        self.dl_item_bar = QProgressBar()
        self.dl_item_bar.setFixedHeight(8); self.dl_item_bar.setRange(0,100)
        self.dl_item_bar.setFormat("%p%")
        self.dl_item_bar.setVisible(False); lay.addWidget(self.dl_item_bar)

        self.lbl_dl_current = QLabel(""); self.lbl_dl_current.setObjectName("subtext")
        lay.addWidget(self.lbl_dl_current)

        # ── Buttons row ────────────────────────────────────────────────────
        brow = QHBoxLayout()
        self.btn_dl_all = QPushButton("⬇  Download All with M3U8")
        self.btn_dl_all.setObjectName("success"); self.btn_dl_all.setFixedHeight(40)
        self.btn_dl_all.clicked.connect(self._dl_all)

        self.btn_dl_new = QPushButton("⬇  Download New Only")
        self.btn_dl_new.setObjectName("success"); self.btn_dl_new.setFixedHeight(40)
        self.btn_dl_new.clicked.connect(self._dl_new)

        self.btn_dl_sel = QPushButton("⬇  Download Selected")
        self.btn_dl_sel.setObjectName("neutral"); self.btn_dl_sel.setFixedHeight(40)
        self.btn_dl_sel.clicked.connect(self._dl_selected)

        self.btn_dl_stop = QPushButton("⏹  Stop")
        self.btn_dl_stop.setObjectName("danger"); self.btn_dl_stop.setFixedHeight(40)
        self.btn_dl_stop.setEnabled(False); self.btn_dl_stop.clicked.connect(self._dl_stop)

        opn = QPushButton("📂  Open Folder"); opn.setObjectName("neutral"); opn.setFixedHeight(40)
        opn.clicked.connect(self._open_dl_folder)

        for b in (self.btn_dl_all, self.btn_dl_new, self.btn_dl_sel, self.btn_dl_stop, opn):
            brow.addWidget(b)
        lay.addLayout(brow)

        # ── Download queue table ───────────────────────────────────────────
        lbl = QLabel("Download Queue"); lbl.setStyleSheet("color:#6c7086; font-size:11px; font-weight:600;")
        lay.addWidget(lbl)

        DL_COLS = [("Title",70), ("Status",90), ("Progress",160), ("File",300)]
        self.dl_table = QTableWidget()
        self.dl_table.setColumnCount(len(DL_COLS))
        self.dl_table.setHorizontalHeaderLabels([c[0] for c in DL_COLS])
        dh = self.dl_table.horizontalHeader()
        dh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, len(DL_COLS)):
            dh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.dl_table.setAlternatingRowColors(True)
        self.dl_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.dl_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.dl_table.verticalHeader().setVisible(False)
        self.dl_table.setShowGrid(False)
        self.dl_table.doubleClicked.connect(self._dl_open_file)
        self.dl_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.dl_table.customContextMenuRequested.connect(self._dl_context_menu)
        lay.addWidget(self.dl_table, 1)

        # Log
        lbl2 = QLabel("Log"); lbl2.setStyleSheet("color:#6c7086; font-size:11px; font-weight:600;")
        lay.addWidget(lbl2)
        self.dl_log = QTextEdit(); self.dl_log.setReadOnly(True)
        self.dl_log.setMaximumHeight(140)
        self.dl_log.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap); lay.addWidget(self.dl_log)

        return w

    # ── Export Tab ──────────────────────────────────────────────────────────

    def _build_export_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(24,20,24,20); lay.setSpacing(14); lay.addStretch()

        lbl = QLabel("Export Collected Data")
        lbl.setStyleSheet("font-size:18px; font-weight:700; color:#cdd6f4;"); lay.addWidget(lbl)
        lay.addWidget(self._sub("All exports go to your configured output directory.")); lay.addSpacing(16)

        for text, obj, fn in [
            ("📄  M3U8 URLs only  (.txt)",            "success", self._export_txt),
            ("📋  Full metadata  (.json)",             "success", self._export_json),
            ("🎵  Media player playlist  (.m3u)",     "success", self._export_m3u),
            ("📊  Spreadsheet  (.csv — all fields)",  "success", self._export_csv),
            ("📂  Export all four formats at once",   None,      self._export_all),
        ]:
            btn = QPushButton(text)
            if obj: btn.setObjectName(obj)
            btn.setFixedHeight(44); btn.setFixedWidth(440); btn.clicked.connect(fn); lay.addWidget(btn)

        lay.addSpacing(14)
        self.lbl_export_status = QLabel("")
        self.lbl_export_status.setStyleSheet("color:#a6e3a1; font-weight:600;"); lay.addWidget(self.lbl_export_status)
        lay.addStretch()
        return w

    # ── Crawl Controls ──────────────────────────────────────────────────────

    def _collect_config(self):
        # Collect active profile names
        active_names = []
        if hasattr(self, '_profile_checks'):
            active_names = [n for n, c in self._profile_checks.items() if c.isChecked()]
        cfg = {
            'profiles':     active_names or ['Artlist'],
            'start_url':    self.inp_url.text().strip(),
            'batch_size':   self.spin_batch_size.value() if hasattr(self, 'spin_batch_size') else 50,
            'page_delay':   self.spin_page_delay.value(),
            'scroll_delay': self.spin_scroll_delay.value(),
            'm3u8_wait':    self.spin_m3u8_wait.value(),
            'scroll_steps': self.spin_scroll_steps.value(),
            'timeout':      self.spin_timeout.value(),
            'max_pages':    self.spin_max_pages.value(),
            'max_depth':    self.spin_max_depth.value(),
            'headless':     self.chk_headless.isChecked(),
            'resume':       self.chk_resume.isChecked(),
            'output_dir':   self.inp_output.text().strip(),
            'dl_dir':       self.inp_dl_dir.text().strip(),
        }
        # v0.3.0 download settings
        if hasattr(self, 'spin_concurrent'):
            cfg['concurrent'] = self.spin_concurrent.value()
        if hasattr(self, 'spin_max_retries'):
            cfg['max_retries'] = self.spin_max_retries.value()
        if hasattr(self, 'spin_bw_limit'):
            cfg['bw_limit'] = self.spin_bw_limit.value()
        # Clipboard monitor state
        if hasattr(self, '_clipboard_timer'):
            cfg['clipboard_monitor'] = self._clipboard_timer.isActive()
        # Filename template
        if hasattr(self, 'inp_fn_template'):
            cfg['fn_template'] = self.inp_fn_template.text().strip() or '{title}'
        return cfg

    # ── Browser management ──────────────────────────────────────────────────

    def _check_browser_status(self):
        """Show/hide browser warning banner based on whether Chromium is ready."""
        ready = _chromium_is_ready()
        self.browser_banner.setVisible(not ready)
        self.btn_start.setEnabled(ready)
        if ready:
            self.status_bar.showMessage("Browser ready.")
        else:
            self.status_bar.showMessage(
                "Chromium not found. Click 'Install Browser' on the Crawl tab.", 8000)
        return ready

    def _install_browser(self):
        """Run playwright install chromium in a background thread with live log."""
        self.btn_install_browser.setEnabled(False)
        self.btn_install_browser.setText("Installing...")
        self.lbl_browser_status.setText("Installing Chromium browser, please wait...")
        self.lbl_browser_status.setStyleSheet("color:#f9e2af; font-weight:600;")
        self.tabs.setCurrentIndex(1)  # Switch to Crawl tab so user sees output

        self._browser_worker = BrowserInstallWorker()
        self._browser_worker.log_signal.connect(
            lambda msg: self._on_log(msg, "INFO"))
        self._browser_worker.finished.connect(self._on_browser_install_done)
        self._browser_worker.start()

    def _on_browser_install_done(self, success):
        self.btn_install_browser.setEnabled(True)
        if success:
            self.btn_install_browser.setText("Reinstall")
            self.lbl_browser_status.setText("Chromium installed successfully!")
            self.lbl_browser_status.setStyleSheet("color:#a6e3a1; font-weight:600;")
            self._check_browser_status()
        else:
            self.btn_install_browser.setText("Retry Install")
            self.lbl_browser_status.setText(
                "Install failed — check the log above. Try running: python -m playwright install chromium")
            self.lbl_browser_status.setStyleSheet("color:#f38ba8; font-weight:600;")

    def _start_crawl(self):
        if self.worker and self.worker.isRunning(): return

        # Pre-flight: verify browser is installed
        if not _chromium_is_ready():
            self._check_browser_status()
            self._on_log(
                "Cannot start: Chromium browser not installed. Click 'Install Browser'.", "ERROR")
            return

        cfg = self._collect_config(); save_config(cfg)
        active_profiles = []
        if hasattr(self, '_profile_checks'):
            for name, chk in self._profile_checks.items():
                if chk.isChecked():
                    p = SiteProfile.get(name)
                    if p: active_profiles.append(p)
        if not active_profiles:
            active_profiles = [self._get_active_profile()]
        self._active_profile = active_profiles[0]
        names = ', '.join(p.name for p in active_profiles)
        self._on_log(f"Starting with profiles: {names} (batch={cfg.get('batch_size', 50)})", "INFO")
        self.worker = CrawlerWorker(cfg, self.db, profiles=active_profiles)
        self.worker.log_signal.connect(self._on_log)
        self.worker.stats_signal.connect(self._on_stats)
        self.worker.clip_signal.connect(self._on_clip_found)
        self.worker.status_signal.connect(self._on_status)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.tabs.setCurrentIndex(1)

        # Auto-start download worker if the checkbox is enabled
        if self.chk_auto_dl.isChecked():
            self._ensure_dl_worker_running()

    def _toggle_pause(self):
        if not self.worker: return
        if self.worker._pause.is_set():
            self.worker.resume(); self.btn_pause.setText("⏸  Pause")
            self._set_status("Running", "#f9e2af")
        else:
            self.worker.pause(); self.btn_pause.setText("▶  Resume")
            self._set_status("Paused", "#cba6f7")

    def _stop_crawl(self):
        if self.worker: self.worker.stop()

    def _on_finished(self):
        self.btn_pause.setEnabled(False); self.btn_stop.setEnabled(False)
        self.btn_pause.setText("⏸  Pause"); self.progress_bar.setVisible(False)
        self._check_browser_status()
        self._refresh_filter_dropdowns(); self._do_search()
        clip_count = self.db.clip_count() if self.db else 0
        q = self._dl_worker.queue_size() if self._dl_worker else 0
        if q > 0:
            msg = f"Crawl complete -- {clip_count} clips found, {q} download(s) in queue."
        else:
            msg = f"Crawl complete -- {clip_count} clips in database."
        self.status_bar.showMessage(msg)
        self._toast(msg, 'success', 4000)
        if hasattr(self, '_tray') and self._tray and self._tray.isVisible() and self.isHidden():
            self._tray.showMessage("Crawl Complete", msg,
                QSystemTrayIcon.MessageIcon.Information, 4000)

    # ── Signal Handlers ─────────────────────────────────────────────────────

    def _on_log(self, msg, level):
        # Filter DEBUG messages based on verbose checkbox
        if level == 'DEBUG' and hasattr(self, 'chk_verbose_log') and not self.chk_verbose_log.isChecked():
            return
        clr = {
            'M3U8':'#a6e3a1','OK':'#a6e3a1','ERROR':'#f38ba8',
            'WARN':'#f9e2af','DEBUG':'#7f849c',
        }.get(level,'#cdd6f4')
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_view.append(
            f'<span style="color:{clr};font-family:Consolas,monospace;font-size:12px;">'
            f'[{ts}] {msg}</span>')
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)
        self._trim_log(self.log_view, 5000)

    def _on_stats(self, s):
        self.stat_clips.setText(str(s.get('clips', 0)))
        self.stat_m3u8.setText(str(s.get('m3u8', 0)))
        self.stat_pages.setText(str(s.get('processed', 0)))
        self.stat_queued.setText(str(s.get('queued', 0)))
        self.stat_errors.setText(str(s.get('failed', 0)))
        self.lbl_clips_hdr.setText(f"Clips: {s.get('clips',0)}")
        self.lbl_m3u8_hdr.setText(f"M3U8: {s.get('m3u8',0)}")

    def _on_status(self, status):
        c = {'running':'#f9e2af','stopped':'#6c7086','challenge':'#f38ba8'}.get(status,'#6c7086')
        l = {'running':'Running','stopped':'Idle','challenge':'Challenge'}.get(status,'Idle')
        self._set_status(l, c)

    def _set_status(self, text, color):
        self.lbl_status_hdr.setText(f"● {text}")
        self.lbl_status_hdr.setStyleSheet(
            f"font-size:12px; font-weight:600; background:transparent; color:{color};")

    def _update_stats(self):
        try:
            if self.db: self._on_stats(self.db.stats())
        except Exception:
            pass

    # ── Search ──────────────────────────────────────────────────────────────

    def _do_search(self):
        """Run search — debounced via QTimer to prevent GUI freeze from rapid typing."""
        if not hasattr(self, '_search_timer'):
            self._search_timer = QTimer()
            self._search_timer.setSingleShot(True)
            self._search_timer.timeout.connect(self._do_search_impl)
        self._search_timer.start(150)  # 150ms debounce

    def _do_search_impl(self):
        query = self.inp_search.text().strip()
        filters = {}
        for attr, col in self._filter_map.items():
            val = getattr(self, attr).currentText()
            if val and val != 'All':
                filters[col] = val

        # Asset management filters
        mode = 'AND' if (hasattr(self, 'btn_search_mode') and self.btn_search_mode.isChecked()) else 'OR'
        favorites_only = hasattr(self, 'chk_favorites') and self.chk_favorites.isChecked()
        downloaded_only = hasattr(self, 'chk_downloaded') and self.chk_downloaded.isChecked()
        duration_range = self.combo_duration.currentText() if hasattr(self, 'combo_duration') else 'All'
        min_rating = self.spin_min_rating.value() if hasattr(self, 'spin_min_rating') else 0

        # Collection filter
        collection_id = None
        if hasattr(self, 'combo_user_collection'):
            cname = self.combo_user_collection.currentText()
            if cname and cname != 'All':
                clean_name = re.sub(r'\s*\(\d+\)$', '', cname)
                try:
                    coll = self.db.execute("SELECT id FROM collections WHERE name=?", (clean_name,)).fetchone()
                    if coll: collection_id = coll['id']
                except Exception: pass

        # Run DB query in background to keep GUI responsive
        def _query():
            rows = self.db.search_assets(
                query=query, filters=filters, mode=mode,
                favorites_only=favorites_only, downloaded_only=downloaded_only,
                duration_range=duration_range, collection_id=collection_id,
                min_rating=min_rating)
            # Convert sqlite3.Row to plain dicts for thread-safe GUI consumption
            return DB._rows_to_dicts(rows) or []

        def _on_results(rows):
            self._last_rows = rows
            self._populate_cards(rows)
            n = len(rows)
            self.lbl_result_count.setText(f"{n} clip{'s' if n!=1 else ''}")
            if not (self._thumb_worker and self._thumb_worker.isRunning()):
                self._start_thumb_worker()

        w = BackgroundWorker(_query)
        w.result_signal.connect(_on_results)
        w.error_signal.connect(lambda e: self.status_bar.showMessage(f"Search error: {e}", 5000))
        self._bg_workers.append(w)
        w.finished.connect(lambda: self._bg_workers.remove(w) if w in self._bg_workers else None)
        w.start()

    def _toggle_search_mode(self):
        checked = self.btn_search_mode.isChecked()
        self.btn_search_mode.setText("AND" if checked else "OR")
        self._do_search()

    def _save_current_search(self):
        query = self.inp_search.text().strip()
        if not query:
            self._toast("Enter a search query first", 'warning', 2000)
            return
        filters = {}
        for attr, col in self._filter_map.items():
            val = getattr(self, attr).currentText()
            if val and val != 'All':
                filters[col] = val
        if hasattr(self, 'chk_favorites') and self.chk_favorites.isChecked():
            filters['_favorites'] = True
        if hasattr(self, 'chk_downloaded') and self.chk_downloaded.isChecked():
            filters['_downloaded'] = True
        if hasattr(self, 'combo_duration') and self.combo_duration.currentText() != 'All':
            filters['_duration'] = self.combo_duration.currentText()
        if hasattr(self, 'spin_min_rating') and self.spin_min_rating.value() > 0:
            filters['_min_rating'] = self.spin_min_rating.value()
        self.db.save_search(query[:50], query, json.dumps(filters))
        self._refresh_saved_searches()
        self._toast(f"Saved search: {query[:40]}", 'success', 2000)

    def _load_saved_search(self, idx):
        if idx == 0: return  # "Saved Searches..." label
        try:
            searches = self.db.get_saved_searches()
            if idx - 1 < len(searches):
                s = searches[idx - 1]
                self.inp_search.setText(s['query'])
                filters = json.loads(s['filters']) if s['filters'] else {}
                # Restore filters
                for attr, col in self._filter_map.items():
                    cb = getattr(self, attr)
                    val = filters.get(col, 'All')
                    idx_f = cb.findText(val)
                    if idx_f >= 0: cb.setCurrentIndex(idx_f)
                if hasattr(self, 'chk_favorites'):
                    self.chk_favorites.setChecked(filters.get('_favorites', False))
                if hasattr(self, 'chk_downloaded'):
                    self.chk_downloaded.setChecked(filters.get('_downloaded', False))
                if hasattr(self, 'combo_duration'):
                    dur = filters.get('_duration', 'All')
                    di = self.combo_duration.findText(dur)
                    if di >= 0: self.combo_duration.setCurrentIndex(di)
                if hasattr(self, 'spin_min_rating'):
                    self.spin_min_rating.setValue(filters.get('_min_rating', 0))
                self._do_search()
        except Exception: pass

    def _refresh_saved_searches(self):
        if not hasattr(self, 'combo_saved_search'): return
        self.combo_saved_search.blockSignals(True)
        self.combo_saved_search.clear()
        self.combo_saved_search.addItem("Saved Searches...")
        try:
            for s in self.db.get_saved_searches():
                self.combo_saved_search.addItem(f"{s['name']}")
        except Exception: pass
        self.combo_saved_search.blockSignals(False)

    def _refresh_collections_combo(self):
        """Refresh the collection filter dropdown."""
        if not hasattr(self, 'combo_user_collection'): return
        self.combo_user_collection.blockSignals(True)
        current = self.combo_user_collection.currentText()
        self.combo_user_collection.clear()
        self.combo_user_collection.addItem("All")
        try:
            for c in self.db.get_collections():
                count = self.db.collection_clip_count(c['id'])
                self.combo_user_collection.addItem(f"{c['name']} ({count})")
        except Exception: pass
        # Restore selection
        idx = self.combo_user_collection.findText(current)
        if idx >= 0: self.combo_user_collection.setCurrentIndex(idx)
        self.combo_user_collection.blockSignals(False)

    def _populate_cards(self, rows):
        # Clean up hover video players before removing cards
        for card in self._current_cards:
            if hasattr(card, 'cleanup_hover'):
                card.cleanup_hover()
            self._card_flow.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._current_cards.clear()
        self._selected_card = None
        # Remove old "Load More" button if present
        if hasattr(self, '_load_more_btn') and self._load_more_btn:
            self._card_flow.removeWidget(self._load_more_btn)
            self._load_more_btn.setParent(None)
            self._load_more_btn.deleteLater()
            self._load_more_btn = None

        self._card_rows_all = list(rows)
        self._card_show_count = 0
        self._append_cards(200)

    _CARD_PAGE_SIZE = 200

    def _append_cards(self, count):
        """Append the next `count` cards from _card_rows_all."""
        # Remove old Load More button before appending
        if hasattr(self, '_load_more_btn') and self._load_more_btn:
            self._card_flow.removeWidget(self._load_more_btn)
            self._load_more_btn.setParent(None)
            self._load_more_btn.deleteLater()
            self._load_more_btn = None

        size_idx = self.card_size_slider.value() if hasattr(self, 'card_size_slider') else 1
        thumb_dir = self._thumb_dir()
        start = self._card_show_count
        end = min(start + count, len(self._card_rows_all))
        for row in self._card_rows_all[start:end]:
            card = ClipCard(row, size_idx=size_idx, thumb_dir=thumb_dir)
            card.tag_clicked.connect(self._on_tag_clicked)
            card.mousePressEvent = lambda e, r=row, c=card: self._on_card_press(e, r, c)
            self._card_flow.addWidget(card)
            self._current_cards.append(card)
        self._card_show_count = end

        # Add "Load More" button if there are remaining cards
        remaining = len(self._card_rows_all) - self._card_show_count
        if remaining > 0:
            btn = QPushButton(f"Load {min(remaining, self._CARD_PAGE_SIZE)} more  ({remaining} remaining)")
            btn.setObjectName("neutral")
            btn.setFixedHeight(36); btn.setFixedWidth(300)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda: self._append_cards(self._CARD_PAGE_SIZE))
            self._card_flow.addWidget(btn)
            self._load_more_btn = btn
        else:
            self._load_more_btn = None

        # Force layout recalc
        self._card_container.adjustSize()
        n = len(self._card_rows_all)
        shown = self._card_show_count
        suffix = f" (showing {shown})" if shown < n else ""
        self.lbl_result_count.setText(f"{n} clip{'s' if n!=1 else ''}{suffix}")

    def _thumb_dir(self):
        base = self.inp_output.text().strip() if hasattr(self, 'inp_output') else ''
        if not base: base = os.path.join(os.path.expanduser('~'), 'ArtlistScraper', 'output')
        d = os.path.join(base, 'thumbs')
        os.makedirs(d, exist_ok=True)
        return d

    def _start_thumb_worker(self):
        clips = self.db.get_clips_needing_thumbs(limit=500)
        if not clips:
            self.status_bar.showMessage("All thumbnails up to date.", 3000)
            return
        # Convert to plain dicts for thread-safe passing
        clips = DB._rows_to_dicts(clips) or []
        thumb_dir = self._thumb_dir()
        self._thumb_worker = ThumbnailWorker(clips, thumb_dir, self.db)
        self._thumb_worker.thumb_ready.connect(self._on_thumb_ready)
        self._thumb_worker.all_done.connect(self._on_thumbs_all_done)
        self._thumb_worker.start()
        self.btn_fetch_thumbs.setText(f"Fetching {len(clips)}...")
        self.btn_fetch_thumbs.setEnabled(False)

    def _on_thumb_ready(self, clip_id, thumb_path):
        # Update any matching card in the current view
        for card in self._current_cards:
            if card._clip_id == clip_id:
                card.set_thumb(thumb_path)
                break

    def _on_thumbs_all_done(self):
        self.btn_fetch_thumbs.setText("Fetch Thumbnails")
        self.btn_fetch_thumbs.setEnabled(True)
        self.status_bar.showMessage("Thumbnails ready.", 3000)

    def _refresh_filter_dropdowns(self):
        for attr, col in self._filter_map.items():
            cb = getattr(self, attr); cur = cb.currentText()
            cb.blockSignals(True); cb.clear(); cb.addItem("All")
            for val in self.db.distinct_values(col): cb.addItem(val)
            idx = cb.findText(cur); cb.setCurrentIndex(idx if idx>=0 else 0)
            cb.blockSignals(False)
        self._refresh_collections_combo()
        self._refresh_saved_searches()

    def _clear_search(self):
        self.inp_search.clear()
        for attr in self._filter_map: getattr(self, attr).setCurrentIndex(0)
        if hasattr(self, 'combo_duration'): self.combo_duration.setCurrentIndex(0)
        if hasattr(self, 'combo_user_collection'): self.combo_user_collection.setCurrentIndex(0)
        if hasattr(self, 'chk_favorites'): self.chk_favorites.setChecked(False)
        if hasattr(self, 'chk_downloaded'): self.chk_downloaded.setChecked(False)
        if hasattr(self, 'spin_min_rating'): self.spin_min_rating.setValue(0)
        if hasattr(self, 'btn_search_mode'):
            self.btn_search_mode.setChecked(False)
            self.btn_search_mode.setText("OR")
        self._do_search()

    # ── Export ──────────────────────────────────────────────────────────────

    def _out_dir(self):
        d = self.inp_output.text().strip() if hasattr(self,'inp_output') else ''
        if not d: d = os.path.join(os.path.expanduser('~'),'ArtlistScraper','output')
        os.makedirs(d, exist_ok=True); return d

    def _ts(self): return datetime.now().strftime('%Y%m%d-%H%M%S')

    def _export_txt(self):
        self.lbl_export_status.setText("Exporting TXT...")
        def _run():
            rows = self.db.all_clips()
            urls = [r['m3u8_url'] for r in rows if r['m3u8_url']]
            if not urls: return "No video URL data."
            f = os.path.join(self._out_dir(), f"video-urls-{self._ts()}.txt")
            with open(f,'w') as fh: fh.write('\n'.join(urls)+'\n')
            return f"Saved {len(urls)} URLs  ->  {f}"
        w = BackgroundWorker(_run)
        w.result_signal.connect(lambda msg: self.lbl_export_status.setText(msg))
        w.error_signal.connect(lambda e: self.lbl_export_status.setText(f"Export error: {e}"))
        self._bg_workers.append(w)
        w.finished.connect(lambda: self._bg_workers.remove(w) if w in self._bg_workers else None)
        w.start()

    def _export_json(self):
        self.lbl_export_status.setText("Exporting JSON...")
        def _run():
            rows = self.db.all_clips()
            if not rows: return "No data."
            f = os.path.join(self._out_dir(), f"video-metadata-{self._ts()}.json")
            with open(f,'w') as fh:
                json.dump({'exported':datetime.now().isoformat(),'total':len(rows),
                           'clips':[dict(r) for r in rows]}, fh, indent=2)
            return f"Saved {len(rows)} clips  ->  {f}"
        w = BackgroundWorker(_run)
        w.result_signal.connect(lambda msg: self.lbl_export_status.setText(msg))
        w.error_signal.connect(lambda e: self.lbl_export_status.setText(f"Export error: {e}"))
        self._bg_workers.append(w)
        w.finished.connect(lambda: self._bg_workers.remove(w) if w in self._bg_workers else None)
        w.start()

    def _export_m3u(self):
        self.lbl_export_status.setText("Exporting M3U...")
        def _run():
            rows = self.db.all_clips()
            lines = ['#EXTM3U']
            for r in rows:
                keys = r.keys()
                local_p = str(r['local_path'] if 'local_path' in keys and r['local_path'] else '')
                m3u8    = str(r['m3u8_url']   if 'm3u8_url'   in keys and r['m3u8_url']   else '')
                title   = str(r['title'] if 'title' in keys and r['title'] else r['clip_id'] or 'Video Clip')
                url = local_p if (local_p and os.path.isfile(local_p)) else m3u8
                if url:
                    lines += [f"#EXTINF:-1,{title}", url]
            if len(lines) == 1: return "No video URL data."
            f = os.path.join(self._out_dir(), f"video-playlist-{self._ts()}.m3u")
            with open(f,'w') as fh: fh.write('\n'.join(lines)+'\n')
            return f"Saved  ->  {f}"
        w = BackgroundWorker(_run)
        w.result_signal.connect(lambda msg: self.lbl_export_status.setText(msg))
        w.error_signal.connect(lambda e: self.lbl_export_status.setText(f"Export error: {e}"))
        self._bg_workers.append(w)
        w.finished.connect(lambda: self._bg_workers.remove(w) if w in self._bg_workers else None)
        w.start()

    def _export_csv(self):
        self.lbl_export_status.setText("Exporting CSV...")
        def _run():
            import csv
            rows = self.db.all_clips()
            if not rows: return "No data."
            f = os.path.join(self._out_dir(), f"video-metadata-{self._ts()}.csv")
            fields = ['clip_id','title','creator','collection','tags','resolution',
                      'duration','frame_rate','camera','formats','m3u8_url','source_url','found_at']
            with open(f,'w',newline='',encoding='utf-8') as fh:
                wr = csv.DictWriter(fh, fieldnames=fields, extrasaction='ignore')
                wr.writeheader()
                for r in rows: wr.writerow({k: (r[k] if k in r.keys() else '') for k in fields})
            return f"Saved {len(rows)} rows  ->  {f}"
        w = BackgroundWorker(_run)
        w.result_signal.connect(lambda msg: self.lbl_export_status.setText(msg))
        w.error_signal.connect(lambda e: self.lbl_export_status.setText(f"Export error: {e}"))
        self._bg_workers.append(w)
        w.finished.connect(lambda: self._bg_workers.remove(w) if w in self._bg_workers else None)
        w.start()

    def _export_all(self):
        self._export_txt(); self._export_json(); self._export_m3u(); self._export_csv()
        msg = f"All 4 formats exporting  --  {self.db.clip_count()} clips"
        self._toast(msg, 'success', 3000)

    # ── Live search update ──────────────────────────────────────────────────

    def _on_clip_found(self, data):
        """Called via clip_signal when a clip is fully processed (has title + best URL)."""
        if not self._clip_found_timer.isActive():
            self._clip_found_timer.start(1000)
        # Auto-download: only if clip has BOTH title and video URL
        if self.chk_auto_dl.isChecked() and data.get('m3u8_url') and data.get('title'):
            self._ensure_dl_worker_running()
            added = self._dl_worker.enqueue(data)
            if added:
                self._add_dl_table_row(data)
                self._update_overall_bar()

    # ── Context Menus ─────────────────────────────────────────────────────────

    def _ctx_toggle_favorites(self, clip_ids):
        for cid in clip_ids:
            self.db.toggle_favorite(cid)
        self._toast(f"Toggled favorites for {len(clip_ids)} clip(s)", 'success', 2000)
        self._do_search()

    def _ctx_set_rating(self, clip_ids, rating):
        for cid in clip_ids:
            self.db.set_rating(cid, rating)
        self._toast(f"Set {rating}\u2605 for {len(clip_ids)} clip(s)", 'success', 2000)
        self._do_search()

    def _ctx_add_to_collection(self, clip_ids, collection_id):
        for cid in clip_ids:
            self.db.add_to_collection(cid, collection_id)
        self._refresh_collections_combo()
        self._toast(f"Added {len(clip_ids)} clip(s) to collection", 'success', 2000)

    def _ctx_new_collection(self, clip_ids):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "New Collection", "Collection name:")
        if ok and name.strip():
            coll_id = self.db.create_collection(name.strip())
            if coll_id:
                for cid in clip_ids:
                    self.db.add_to_collection(cid, coll_id)
                self._refresh_collections_combo()
                self._toast(f"Created '{name.strip()}' with {len(clip_ids)} clip(s)", 'success', 2000)

    def _dl_context_menu(self, pos):
        """Right-click context menu for the download queue table."""
        menu = QMenu(self)

        rows = sorted({idx.row() for idx in self.dl_table.selectedIndexes()})

        if rows:
            # Open file
            act_open = menu.addAction("Open File")
            act_open.triggered.connect(lambda: self._dl_open_file(self.dl_table.indexFromItem(
                self.dl_table.item(rows[0], 0))))

            # Open containing folder
            act_folder = menu.addAction("Open Containing Folder")
            act_folder.triggered.connect(lambda: self._ctx_dl_open_folder(rows[0]))

            # Copy filename
            act_copy = menu.addAction("Copy Filename")
            act_copy.triggered.connect(lambda: self._ctx_copy_dl_column(rows, 3))

            menu.addSeparator()

            # Retry if error
            act_retry = menu.addAction("Retry Failed")
            act_retry.triggered.connect(self._retry_all_errors)
        else:
            act_open_dir = menu.addAction("Open Download Folder")
            act_open_dir.triggered.connect(self._open_dl_folder)

        menu.exec(self.dl_table.viewport().mapToGlobal(pos))

    def _ctx_dl_open_folder(self, row):
        """Open the containing folder for a download table row."""
        item = self.dl_table.item(row, 3)
        if not item: return
        path = item.data(_LOCAL_PATH_ROLE) or item.text()
        if path and os.path.isfile(path):
            folder = os.path.dirname(path)
            try:
                if sys.platform == 'win32': subprocess.Popen(['explorer', '/select,', path])
                elif sys.platform == 'darwin': subprocess.Popen(['open', '-R', path])
                else: subprocess.Popen(['xdg-open', folder])
            except Exception: pass

    def _ctx_copy_dl_column(self, rows, col_idx):
        """Copy values from download table column."""
        vals = []
        for r in rows:
            item = self.dl_table.item(r, col_idx)
            if item and item.text(): vals.append(item.text())
        if vals:
            QApplication.clipboard().setText('\n'.join(vals))
            self._toast(f"Copied {len(vals)} value(s)", 'success', 2000)

    # ── Download Controls ────────────────────────────────────────────────────

    def _dl_dir(self):
        d = self.inp_dl_dir.text().strip()
        if not d:
            base = self.inp_output.text().strip() if hasattr(self,'inp_output') else ''
            if not base: base = os.path.join(os.path.expanduser('~'), 'ArtlistScraper', 'output')
            d = os.path.join(base, 'downloads')
        os.makedirs(d, exist_ok=True)
        return d

    def _browse_dl_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select Download Directory", self._dl_dir())
        if d: self.inp_dl_dir.setText(d)

    def _open_dl_folder(self):
        d = self._dl_dir()
        os.makedirs(d, exist_ok=True)
        try:
            if sys.platform == 'win32':  os.startfile(d)
            elif sys.platform == 'darwin': subprocess.Popen(['open', d])
            else:                         subprocess.Popen(['xdg-open', d])
        except Exception: pass

    def _update_dl_stats(self):
        try:
            if not self.db: return
            total   = self.db.execute("SELECT COUNT(*) FROM clips WHERE m3u8_url != ''").fetchone()[0]
            done    = self.db.execute("SELECT COUNT(*) FROM clips WHERE dl_status='done'").fetchone()[0]
            errors  = self.db.execute("SELECT COUNT(*) FROM clips WHERE dl_status='error'").fetchone()[0]
            pending = total - done - errors
            self.lbl_dl_queue.setText(f"Ready: {pending}")
            self.lbl_dl_done.setText(f"Downloaded: {done}")
            self.lbl_dl_errors.setText(f"Errors: {errors}")
        except Exception:
            pass

    def _ensure_dl_worker_running(self):
        """Create and start the persistent DL worker if not already running."""
        if self._dl_worker and self._dl_worker.isRunning():
            # Update output dir and filename template in case they changed
            self._dl_worker.out_dir = self._dl_dir()
            self._dl_worker._fn_template = (load_config() or {}).get('fn_template', '{title}')
            return
        concurrent = self.spin_concurrent.value() if hasattr(self, 'spin_concurrent') else 2
        max_retries = self.spin_max_retries.value() if hasattr(self, 'spin_max_retries') else 3
        self._dl_worker = DownloadWorker(
            self._dl_dir(), self.db,
            max_concurrent=concurrent,
            max_retries=max_retries)
        self._dl_worker.log_signal.connect(self._on_dl_log)
        self._dl_worker.progress_signal.connect(self._on_dl_progress)
        self._dl_worker.clip_done.connect(self._on_dl_clip_done)
        self._dl_worker.all_done.connect(self._on_dl_all_done)
        self._dl_worker.start()
        self.btn_dl_stop.setEnabled(True)
        self.dl_item_bar.setVisible(True)
        self.dl_overall_bar.setVisible(True)
        self.dl_overall_bar.setTextVisible(True)
        self._dl_done_count = 0

    def _add_dl_table_row(self, clip):
        """Add a row to the download queue table for a clip. Safe to call from main thread only."""
        # sqlite3.Row doesn't have .get() — normalize to dict
        if hasattr(clip, 'keys') and not isinstance(clip, dict):
            clip = dict(zip(clip.keys(), tuple(clip)))
        cid = str(clip.get('clip_id', '') or '')
        if cid in self._dl_clip_rows:
            return   # already in table
        r = self.dl_table.rowCount()
        self.dl_table.insertRow(r)
        self._dl_clip_rows[cid] = r
        self.dl_table.setItem(r, 0, QTableWidgetItem(str(clip.get('title', '') or cid)))
        si = QTableWidgetItem("Queued")
        si.setForeground(QColor('#6c7086')); self.dl_table.setItem(r, 1, si)
        self.dl_table.setItem(r, 2, QTableWidgetItem(""))
        self.dl_table.setItem(r, 3, QTableWidgetItem(""))

    def _update_overall_bar(self):
        total = len(self._dl_clip_rows)
        self.dl_overall_bar.setRange(0, max(total, 1))
        self.dl_overall_bar.setFormat(f"{self._dl_done_count} / {total}")

    def _start_downloads(self, clips):
        """Bulk-enqueue clips into the persistent worker, starting it if needed."""
        if not clips:
            self.status_bar.showMessage("No clips with M3U8 URLs to download.", 4000)
            return
        self._ensure_dl_worker_running()
        added = 0
        for clip in clips:
            if self._dl_worker.enqueue(clip):
                self._add_dl_table_row(clip)
                added += 1
        if added:
            self._update_overall_bar()
            self.status_bar.showMessage(f"Queued {added} clip(s) for download.")
        else:
            self.status_bar.showMessage("All selected clips already queued or downloaded.")
        self.tabs.setCurrentIndex(3)

    def _dl_all(self):
        clips = DB._rows_to_dicts(self.db.clips_with_m3u8(only_undownloaded=False)) or []
        self._start_downloads(clips)

    def _dl_new(self):
        clips = DB._rows_to_dicts(self.db.clips_with_m3u8(only_undownloaded=True)) or []
        self._start_downloads(clips)

    def _dl_selected(self):
        """Download the currently selected clip from the detail panel."""
        if not self._detail_clip:
            self.status_bar.showMessage("Select a clip in the card grid first.", 3000)
            return
        keys = self._detail_clip.keys() if hasattr(self._detail_clip, 'keys') else []
        cid = str(self._detail_clip['clip_id'] if 'clip_id' in keys else '')
        if not cid:
            self.status_bar.showMessage("No clip selected.", 3000)
            return
        clips = self.db.execute(
            "SELECT * FROM clips WHERE clip_id=? AND m3u8_url != ''", (cid,)).fetchall()
        if clips:
            self._start_downloads(DB._rows_to_dicts(clips) or [])
        else:
            self.status_bar.showMessage("Clip has no M3U8 URL.", 3000)

    def _dl_stop(self):
        if self._dl_worker: self._dl_worker.stop()

    def _on_dl_log(self, msg, level):
        clr = {'OK':'#a6e3a1','ERROR':'#f38ba8','WARN':'#f9e2af'}.get(level,'#cdd6f4')
        self.dl_log.append(
            f'<span style="color:{clr};font-family:Consolas;font-size:11px;">'
            f'[{datetime.now().strftime("%H:%M:%S")}] {msg}</span>')
        self.dl_log.moveCursor(QTextCursor.MoveOperation.End)
        self._trim_log(self.dl_log, 2000)

    def _on_dl_progress(self, clip_id, pct, status_text):
        # Track active downloads for concurrent display
        if not hasattr(self, '_active_downloads'):
            self._active_downloads = {}
        if pct >= 100 or status_text.startswith('Done') or status_text.startswith('Error') or status_text.startswith('Failed'):
            self._active_downloads.pop(clip_id, None)
        else:
            self._active_downloads[clip_id] = (pct, status_text)

        # Show aggregate progress on the item bar (average of active downloads)
        if self._active_downloads:
            avg_pct = sum(p for p, _ in self._active_downloads.values()) // len(self._active_downloads)
            self.dl_item_bar.setValue(avg_pct)
            active_count = len(self._active_downloads)
            title = self.dl_table.item(self._dl_clip_rows.get(clip_id, 0), 0)
            title_text = title.text()[:30] if title else clip_id[:12]
            if active_count > 1:
                self.lbl_dl_current.setText(
                    f"{active_count} active  |  {title_text}: {status_text}")
            else:
                self.lbl_dl_current.setText(f"{title_text}: {status_text}")
        else:
            self.dl_item_bar.setValue(pct)
            self.lbl_dl_current.setText(status_text)

        # Update per-clip row in download table
        if clip_id in self._dl_clip_rows:
            r = self._dl_clip_rows[clip_id]
            si = self.dl_table.item(r, 1)
            if si:
                si.setText("Downloading")
                si.setForeground(QColor('#f9e2af'))
            else:
                si = QTableWidgetItem("Downloading")
                si.setForeground(QColor('#f9e2af'))
                self.dl_table.setItem(r, 1, si)
            pi = self.dl_table.item(r, 2)
            if pi:
                pi.setText(status_text)
            else:
                self.dl_table.setItem(r, 2, QTableWidgetItem(status_text))

    def _on_dl_clip_done(self, clip_id, success, path_or_err):
        self._dl_done_count = getattr(self, '_dl_done_count', 0) + 1
        total = self.dl_overall_bar.maximum()
        self.dl_overall_bar.setValue(self._dl_done_count)
        self.dl_overall_bar.setFormat(f"{self._dl_done_count} / {total}")
        if clip_id in self._dl_clip_rows:
            r = self._dl_clip_rows[clip_id]
            if success:
                si = QTableWidgetItem("Done")
                si.setForeground(QColor('#a6e3a1'))
                self.dl_table.setItem(r, 1, si)
                fi = QTableWidgetItem(os.path.basename(path_or_err))
                fi.setForeground(QColor('#89b4fa'))
                fi.setData(_LOCAL_PATH_ROLE, path_or_err)  # store full path
                self.dl_table.setItem(r, 3, fi)
                self.dl_table.setItem(r, 2, QTableWidgetItem("100%"))
            else:
                si = QTableWidgetItem("Error")
                si.setForeground(QColor('#f38ba8'))
                self.dl_table.setItem(r, 1, si)
                self.dl_table.setItem(r, 3, QTableWidgetItem(path_or_err[:60]))
        self._update_dl_stats()
        self._do_search()  # Refresh search table to update Downloaded column
        # Update tray tooltip with progress
        if hasattr(self, '_tray') and self._tray:
            self._tray.setToolTip(f"Artlist Scraper  |  {self._dl_done_count}/{total} downloaded")

    def _on_dl_all_done(self):
        self.btn_dl_stop.setEnabled(False)
        self.dl_item_bar.setVisible(False)
        msg = f"Download complete -- {self._dl_done_count} file(s) downloaded this session."
        self.lbl_dl_current.setText(msg)
        self.status_bar.showMessage(msg, 5000)
        self._update_dl_stats()
        # Toast + tray notification
        self._toast(msg, 'success', 5000)
        if hasattr(self, '_tray') and self._tray and self._tray.isVisible():
            self._tray.showMessage(
                "Downloads Complete",
                f"{self._dl_done_count} clip(s) downloaded successfully.",
                QSystemTrayIcon.MessageIcon.Information, 5000)

    def _dl_open_file(self, index):
        """Double-click a row in the download queue table to open the file."""
        item = self.dl_table.item(index.row(), 3)
        if not item: return
        path = item.data(_LOCAL_PATH_ROLE) or item.text()
        if path and os.path.isfile(path):
            try:
                if sys.platform == 'win32': os.startfile(path)
                elif sys.platform == 'darwin': subprocess.Popen(['open', path])
                else: subprocess.Popen(['xdg-open', path])
            except Exception: pass

    # ── Profile handling ────────────────────────────────────────────────────

    def _on_profile_changed(self, name):
        """Update UI when site profile changes."""
        profile = SiteProfile.get(name)
        if not profile:
            return
        self._active_profile = profile
        self.lbl_profile_desc.setText(profile.description)
        # Set default start URL for the profile (don't overwrite if user typed something)
        if profile.start_url:
            current = self.inp_url.text().strip()
            # Only auto-fill if the current URL belongs to a different profile
            is_different_site = True
            if profile.domains:
                try:
                    cur_domain = urlparse(current).netloc
                    is_different_site = not profile.is_allowed_domain(cur_domain)
                except Exception: pass
            if not current or is_different_site:
                self.inp_url.setText(profile.start_url)

    def _get_active_profile(self):
        if hasattr(self, '_profile_checks'):
            for name, chk in self._profile_checks.items():
                if chk.isChecked():
                    return SiteProfile.get(name)
        return SiteProfile.get('Artlist')

    # ── Config I/O ──────────────────────────────────────────────────────────

    def _save_cfg(self):
        save_config(self._collect_config())
        self.status_bar.showMessage("Configuration saved.")

    def _load_cfg_file(self):
        path, _ = QFileDialog.getOpenFileName(self,"Load Config",get_config_dir(),"JSON (*.json)")
        if path:
            try:
                with open(path) as f: self._apply_config(json.load(f))
            except Exception as e: QMessageBox.critical(self,"Error",str(e))

    def _load_saved_config(self):
        cfg = load_config()
        if cfg: self._apply_config(cfg)

    def _apply_config(self, cfg):
        # Restore profile checkboxes
        if 'profiles' in cfg and hasattr(self, '_profile_checks'):
            for name, chk in self._profile_checks.items():
                chk.setChecked(name in cfg['profiles'])
        elif 'profile' in cfg and hasattr(self, '_profile_checks'):
            # Legacy single-profile config
            for name, chk in self._profile_checks.items():
                chk.setChecked(name == cfg['profile'])
        if 'batch_size' in cfg and hasattr(self, 'spin_batch_size'):
            self.spin_batch_size.setValue(cfg['batch_size'])
        for k, w in [('start_url',self.inp_url),('output_dir',self.inp_output),('dl_dir',self.inp_dl_dir)]:
            if k in cfg: w.setText(cfg[k])
        for k, w in [('page_delay',self.spin_page_delay),('scroll_delay',self.spin_scroll_delay),
                     ('m3u8_wait',self.spin_m3u8_wait),('scroll_steps',self.spin_scroll_steps),
                     ('timeout',self.spin_timeout),('max_pages',self.spin_max_pages),
                     ('max_depth',self.spin_max_depth)]:
            if k in cfg: w.setValue(cfg[k])
        if 'headless' in cfg: self.chk_headless.setChecked(cfg['headless'])
        if 'resume'   in cfg: self.chk_resume.setChecked(cfg['resume'])
        if 'fn_template' in cfg and hasattr(self, 'inp_fn_template'):
            self.inp_fn_template.setText(cfg['fn_template'])
            if hasattr(self, '_update_fn_preview'): self._update_fn_preview()
        # v0.3.0 download settings
        if 'concurrent' in cfg and hasattr(self, 'spin_concurrent'):
            self.spin_concurrent.setValue(cfg['concurrent'])
        if 'max_retries' in cfg and hasattr(self, 'spin_max_retries'):
            self.spin_max_retries.setValue(cfg['max_retries'])
        if 'bw_limit' in cfg and hasattr(self, 'spin_bw_limit'):
            self.spin_bw_limit.setValue(cfg['bw_limit'])
        # Clipboard monitor (opt-in)
        if cfg.get('clipboard_monitor', False) and hasattr(self, '_clipboard_timer'):
            if not self._clipboard_timer.isActive():
                self._clipboard_timer.start(2000)

    def _browse_output(self):
        d = QFileDialog.getExistingDirectory(self,"Select Output Dir",self.inp_output.text())
        if d: self.inp_output.setText(d)

    def _clear_db(self):
        if QMessageBox.question(self,"Clear Database",
                "Delete ALL clips, metadata, M3U8 links and crawl history?",
                QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No
            ) == QMessageBox.StandardButton.Yes:
            self.db.clear_all(); self._do_search(); self._update_stats()
            self.log_view.clear(); self.status_bar.showMessage("Database cleared.")

    def _rebuild_fts(self):
        """Rebuild the full-text search index if search results seem out of sync."""
        count = self.db.rebuild_fts()
        if count >= 0:
            self._toast(f"Search index rebuilt: {count} clips indexed", 'success', 3000)
        else:
            self._toast("Search index rebuild failed — check logs", 'error', 4000)

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _sub(self, t):
        l = QLabel(t); l.setObjectName("subtext"); return l

    @staticmethod
    def _trim_log(text_edit, max_blocks=5000):
        """Trim a QTextEdit to prevent unbounded memory growth during long sessions."""
        doc = text_edit.document()
        if doc.blockCount() > max_blocks:
            cursor = text_edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.movePosition(QTextCursor.MoveOperation.Down,
                                QTextCursor.MoveMode.KeepAnchor, doc.blockCount() - max_blocks + 500)
            cursor.removeSelectedText()
            text_edit.moveCursor(QTextCursor.MoveOperation.End)

    def _on_tab_changed(self, idx):
        try:
            if hasattr(self, 'arc_stat_clips'):
                self._refresh_archive_stats()
        except Exception:
            pass

    def closeEvent(self, event):
        # If tray is available and user didn't explicitly quit, minimize to tray
        if (hasattr(self, '_tray') and self._tray and self._tray.isVisible()
                and not getattr(self, '_tray_quitting', False)):
            event.ignore()
            self.hide()
            self._tray.showMessage(
                "Artlist Scraper", "Still running in background.",
                QSystemTrayIcon.MessageIcon.Information, 2000)
            return
        # Stop timers FIRST so they can't fire against a closed DB
        self._stats_timer.stop()
        self._dl_stats_timer.stop()
        if hasattr(self, '_clipboard_timer'):
            self._clipboard_timer.stop()
        if hasattr(self, '_preview_timer'):
            self._preview_timer.stop()
        if getattr(self, '_video_player', None):
            self._video_player.stop()
        if self.worker and self.worker.isRunning():
            self.worker.stop(); self.worker.wait(3000)
        if self._dl_worker and self._dl_worker.isRunning():
            self._dl_worker.stop(); self._dl_worker.wait(5000)
        if self._thumb_worker and self._thumb_worker.isRunning():
            self._thumb_worker.stop(); self._thumb_worker.wait(3000)
        if hasattr(self, '_tray') and self._tray:
            self._tray.hide()
        if self.db: self.db.close()
        save_config(self._collect_config())
        event.accept()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # High-DPI: environment hints BEFORE QApplication
    os.environ.setdefault('QT_AUTO_SCREEN_SCALE_FACTOR', '1')
    os.environ.setdefault('QT_ENABLE_HIGHDPI_SCALING', '1')
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.RoundPreferFloor)
    app = QApplication(sys.argv)
    _init_dpi()
    app.setStyleSheet(DARK_STYLE)
    app.setApplicationName("Artlist M3U8 Scraper")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
