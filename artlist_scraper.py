# Video Scraper v1.4.0
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
# v0.8.0: Catalog mode — full-width card grid for browsing all clips, sort controls
#          (newest/oldest/title/resolution/duration/rating), XL card size (320x180),
#          slide-in detail panel on card click, close button for catalog detail dismissal,
#          ClipCard keys reference fix
# v0.8.1: Import Folder — scan local video directories into the catalog with
#          ffprobe metadata extraction (resolution, duration, fps), automatic
#          thumbnail generation, parent folder as collection name, stable
#          hash-based clip IDs, recursive scan, background import worker
# v0.8.2: Audit fixes — infinite search loop (timer connected to _do_search
#          instead of _do_search_impl), source_site missing from _VALID_COLUMNS
#          (Source dropdown filter silently rejected), _write_sidecar NameError
#          (clip_data→data), hashlib import moved out of per-file loop
# v0.9.0: Premium theme overhaul — cinema-grade deep dark palette,
#          refined typography and spacing, professional surface hierarchy,
#          ultra-thin scrollbars, glass-effect toasts, premium stat cards,
#          refined card grid styling, updated branding and accent system
# v0.9.1: FTS auto-recovery — startup integrity check auto-rebuilds corrupted
#          FTS index, all FTS writes separated from main data operations,
#          corruption detected at runtime triggers automatic DROP+recreate,
#          search/search_assets gracefully fall back during recovery,
#          clear_all hardened against corrupted FTS tables
# v0.9.2: Artlist crawl fixes — scan no longer dumps 57 related video previews
#          when clip ID not found in URL (was polluting DB with wrong URLs),
#          M3U8 master playlist parsing extracts RESOLUTION= and FRAME-RATE=
#          for Artlist HLS streams (fixes res:? on every clip), response
#          interceptor skips unverifiable URLs after first video captured,
#          JS interceptor skips unverifiable URLs entirely, catalog video
#          extraction falls back to href for clip ID, metadata selectors
#          now handle newlines between label and value
# v1.0.0: UI scaling system -- zoom controls (75%-200%) in header bar,
#          Ctrl+=/Ctrl+-/Ctrl+0 shortcuts, full UI rebuild on zoom change,
#          all setFixedHeight/Width/Size + inline font-sizes use Z() scaling,
#          _build_stylesheet(scale) replaces static DARK_STYLE, zoom persists.
#          Power improvements -- concurrent downloads up to 32 (default 4),
#          retries up to 25, batch size up to 5000, max depth 25, lower
#          minimum delays (200ms page / 50ms scroll / 500ms M3U8), scroll
#          steps up to 200, bandwidth limit up to 500 MB/s.
# v1.1.0: Multi-select cards (Ctrl+click toggle, Shift+click range), bulk
#          context menu ops (rate/fav/collect/download N clips at once),
#          Ctrl+A select all / Escape deselect, selection count in status.
#          Disk space check before downloads (500 MB minimum free).
#          Export current search results (filtered TXT/JSON/M3U/CSV).
#          Lazy batch thumbnail loading (30/frame) prevents UI freeze.
#          WAL checkpoint timer (2 min) prevents unbounded WAL growth.
#          Auto-trim log every 500 appends (was every append).
#          Open Config / DB / Output directory buttons in Archive tab.
#          All remaining hardcoded px values now use Z() scaling.

import sys, os, subprocess, traceback, re, random, shutil

# Hide console window on Windows immediately (before any prints)
if sys.platform == 'win32':
    try:
        import ctypes as _ct
        _hw = _ct.windll.kernel32.GetConsoleWindow()
        if _hw:
            _ct.windll.user32.ShowWindow(_hw, 0)  # SW_HIDE
        del _ct, _hw
    except Exception:
        pass

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
            0, f"Fatal error:\n{crash_file}\n\n{msg[:800]}", "Video Scraper — Fatal Error", 0x10)
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


# Global UI zoom level (1.0 = 100%)
_ui_scale = 1.0

ZOOM_PRESETS = [
    ('75%',  0.75),
    ('90%',  0.90),
    ('100%', 1.00),
    ('110%', 1.10),
    ('125%', 1.25),
    ('150%', 1.50),
    ('175%', 1.75),
    ('200%', 2.00),
]

def Z(px):
    """Scale a pixel value by both DPI factor AND UI zoom level. Returns int.
    Used for all programmatic widget sizing — buttons, cards, margins, fonts.
    """
    return max(1, int(px * _ui_scale * _dpi_factor))


# ─────────────────────────────────────────────────────────────────────────────
# THEME PALETTE SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

# Active palette — referenced by C(key) helper and _build_stylesheet()
_theme_palette = {}

def C(key):
    """Return the active theme color for a semantic key. Like Z() for colors."""
    return _theme_palette.get(key, '#ff00ff')  # magenta = missing key

THEME_PALETTES = {
    'OLED': {
        # Surfaces
        'bg':           '#0c0c12', 'bg_deep':       '#08080e', 'bg_input':      '#0e0e18',
        'bg_card':      '#10101a', 'bg_header':     '#08080e', 'bg_hover':      '#121220',
        'bg_button':    '#18182a', 'bg_card_area':  '#0a0a10', 'bg_panel':      '#0a0a12',
        'bg_tooltip':   '#141420', 'bg_video':      '#06060a',
        # Borders
        'border':       '#1a1a2a', 'border_input':  '#1e1e32', 'border_light':  '#24243a',
        'border_subtle':'#14141e', 'border_card':   '#16162a',
        # Text
        'text':         '#dcdce8', 'text_muted':    '#52526e', 'text_hover':    '#8e8ea8',
        'text_disabled':'#36364e', 'text_soft':     '#b0b0c8', 'text_neutral':  '#a0a0bc',
        # Accent
        'accent':       '#3d8af7', 'accent_hover':  '#5a9ef9', 'accent_pressed':'#2a6fd0',
        'accent_bg':    '#182040', 'accent_grad':   '#38bdf8', 'accent_combo':  '#12121e',
        # Status
        'success':      '#2dd4a8', 'error':         '#e85d75', 'warning':       '#e8a832',
        'danger':       '#c92a2a', 'danger_hover':  '#e03c3c',
        'success_btn':  '#0d8a5e', 'success_btn_h': '#12a674',
        'warning_btn':  '#b8860b', 'warning_btn_h': '#d4a017',
        # Special
        'purple':       '#9580f0', 'purple_hover':  '#b0a0ff',
        'log_text':     '#2dd4a8',
        # Selection
        'sel_border':   '#3d8af7', 'sel_bg':        '#0e1424',
        'multi_border': '#9580f0', 'multi_bg':      '#12102a',
        # Toast backgrounds
        'toast_info_bg':   '#0e1420', 'toast_success_bg': '#0a1a14',
        'toast_warning_bg':'#1a1408', 'toast_error_bg':   '#1a0a0e',
    },
    'Dark': {
        'bg':           '#1a1b26', 'bg_deep':       '#13141c', 'bg_input':      '#1e1f2e',
        'bg_card':      '#1f2030', 'bg_header':     '#13141c', 'bg_hover':      '#252638',
        'bg_button':    '#2a2b3d', 'bg_card_area':  '#161722', 'bg_panel':      '#171824',
        'bg_tooltip':   '#222336', 'bg_video':      '#10111a',
        'border':       '#2e2f44', 'border_input':  '#33345a', 'border_light':  '#3a3b56',
        'border_subtle':'#262738', 'border_card':   '#282940',
        'text':         '#d5d6e8', 'text_muted':    '#6b6d8a', 'text_hover':    '#9a9cb8',
        'text_disabled':'#484a64', 'text_soft':     '#b4b6d0', 'text_neutral':  '#a0a2bc',
        'accent':       '#4d94ff', 'accent_hover':  '#6aa6ff', 'accent_pressed':'#3570d4',
        'accent_bg':    '#1e2a48', 'accent_grad':   '#42c0ff', 'accent_combo':  '#1e1f30',
        'success':      '#34ddb0', 'error':         '#f06080', 'warning':       '#f0b040',
        'danger':       '#d03030', 'danger_hover':  '#e84444',
        'success_btn':  '#109868', 'success_btn_h': '#16b480',
        'warning_btn':  '#c49010', 'warning_btn_h': '#daa820',
        'purple':       '#a090f8', 'purple_hover':  '#bbb0ff',
        'log_text':     '#34ddb0',
        'sel_border':   '#4d94ff', 'sel_bg':        '#1a2438',
        'multi_border': '#a090f8', 'multi_bg':      '#1e1838',
        'toast_info_bg':   '#141e30', 'toast_success_bg': '#102820',
        'toast_warning_bg':'#282010', 'toast_error_bg':   '#281018',
    },
    'Midnight': {
        'bg':           '#0d1117', 'bg_deep':       '#080c10', 'bg_input':      '#111820',
        'bg_card':      '#131a24', 'bg_header':     '#080c10', 'bg_hover':      '#182030',
        'bg_button':    '#1c2638', 'bg_card_area':  '#0a1018', 'bg_panel':      '#0c1218',
        'bg_tooltip':   '#162030', 'bg_video':      '#06080c',
        'border':       '#1e2a3a', 'border_input':  '#243040', 'border_light':  '#2c3a4c',
        'border_subtle':'#162030', 'border_card':   '#1a2434',
        'text':         '#d0d8e8', 'text_muted':    '#4e6080', 'text_hover':    '#8090a8',
        'text_disabled':'#344050', 'text_soft':     '#a0b0c8', 'text_neutral':  '#90a0b8',
        'accent':       '#58a6ff', 'accent_hover':  '#79b8ff', 'accent_pressed':'#3a80d0',
        'accent_bg':    '#162844', 'accent_grad':   '#4ac0f0', 'accent_combo':  '#101820',
        'success':      '#3fb950', 'error':         '#f85149', 'warning':       '#d29922',
        'danger':       '#c42020', 'danger_hover':  '#e03838',
        'success_btn':  '#1a8040', 'success_btn_h': '#239850',
        'warning_btn':  '#b08018', 'warning_btn_h': '#c89820',
        'purple':       '#bc8cff', 'purple_hover':  '#d2a8ff',
        'log_text':     '#3fb950',
        'sel_border':   '#58a6ff', 'sel_bg':        '#0e1a2c',
        'multi_border': '#bc8cff', 'multi_bg':      '#14102c',
        'toast_info_bg':   '#0c1828', 'toast_success_bg': '#0c2018',
        'toast_warning_bg':'#201808', 'toast_error_bg':   '#200c10',
    },
    'Graphite': {
        'bg':           '#2b2b30', 'bg_deep':       '#222226', 'bg_input':      '#303036',
        'bg_card':      '#333338', 'bg_header':     '#222226', 'bg_hover':      '#3a3a40',
        'bg_button':    '#404048', 'bg_card_area':  '#282830', 'bg_panel':      '#292930',
        'bg_tooltip':   '#383840', 'bg_video':      '#1c1c22',
        'border':       '#48484e', 'border_input':  '#505058', 'border_light':  '#5a5a62',
        'border_subtle':'#3e3e46', 'border_card':   '#444450',
        'text':         '#e0e0e8', 'text_muted':    '#88889a', 'text_hover':    '#b0b0c0',
        'text_disabled':'#606070', 'text_soft':     '#c0c0d0', 'text_neutral':  '#a8a8b8',
        'accent':       '#5a9cf5', 'accent_hover':  '#78b0ff', 'accent_pressed':'#4080d0',
        'accent_bg':    '#2a3448', 'accent_grad':   '#48baff', 'accent_combo':  '#343438',
        'success':      '#40d8a8', 'error':         '#f06878', 'warning':       '#f0b840',
        'danger':       '#d03838', 'danger_hover':  '#e84c4c',
        'success_btn':  '#189868', 'success_btn_h': '#20b080',
        'warning_btn':  '#c89818', 'warning_btn_h': '#e0b020',
        'purple':       '#a898f8', 'purple_hover':  '#c0b0ff',
        'log_text':     '#40d8a8',
        'sel_border':   '#5a9cf5', 'sel_bg':        '#2e3440',
        'multi_border': '#a898f8', 'multi_bg':      '#302838',
        'toast_info_bg':   '#242838', 'toast_success_bg': '#1c2c24',
        'toast_warning_bg':'#302818', 'toast_error_bg':   '#301820',
    },
    'Mocha': {
        'bg':           '#1e1e2e', 'bg_deep':       '#181825', 'bg_input':      '#232336',
        'bg_card':      '#262637', 'bg_header':     '#181825', 'bg_hover':      '#2e2e42',
        'bg_button':    '#36364c', 'bg_card_area':  '#1c1c2c', 'bg_panel':      '#1e1e30',
        'bg_tooltip':   '#2c2c40', 'bg_video':      '#141420',
        'border':       '#3e3e58', 'border_input':  '#454560', 'border_light':  '#50506a',
        'border_subtle':'#333348', 'border_card':   '#383850',
        'text':         '#cdd6f4', 'text_muted':    '#6c7086', 'text_hover':    '#9399b2',
        'text_disabled':'#585b70', 'text_soft':     '#bac2de', 'text_neutral':  '#a6adc8',
        'accent':       '#89b4fa', 'accent_hover':  '#a6c8ff', 'accent_pressed':'#6a96d8',
        'accent_bg':    '#1e2c48', 'accent_grad':   '#74c7ec', 'accent_combo':  '#252538',
        'success':      '#a6e3a1', 'error':         '#f38ba8', 'warning':       '#f9e2af',
        'danger':       '#d04040', 'danger_hover':  '#e85858',
        'success_btn':  '#40a060', 'success_btn_h': '#50b870',
        'warning_btn':  '#d0a020', 'warning_btn_h': '#e0b838',
        'purple':       '#cba6f7', 'purple_hover':  '#dcc0ff',
        'log_text':     '#a6e3a1',
        'sel_border':   '#89b4fa', 'sel_bg':        '#1e2438',
        'multi_border': '#cba6f7', 'multi_bg':      '#241e38',
        'toast_info_bg':   '#1a2038', 'toast_success_bg': '#182820',
        'toast_warning_bg':'#282018', 'toast_error_bg':   '#281420',
    },
}

THEME_NAMES = list(THEME_PALETTES.keys())

_active_theme_name = 'OLED'

def _set_theme(name):
    """Activate a theme palette by name."""
    global _theme_palette, _active_theme_name
    _theme_palette = THEME_PALETTES.get(name, THEME_PALETTES['OLED']).copy()
    _active_theme_name = name

# Default theme
_set_theme('OLED')

# ─────────────────────────────────────────────────────────────────────────────
# THEMED STYLESHEET GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

def _build_stylesheet(scale=1.0, theme_name=None):
    """Generate the complete themed stylesheet scaled by the given factor."""
    if theme_name:
        _set_theme(theme_name)
    p = _theme_palette
    sf = scale * _dpi_factor
    def px(base):
        return max(1, int(base * sf))
    return f"""
/* VIDEO SCRAPER -- Premium Theme v1.2.0 | {_active_theme_name} | zoom: {int(scale*100)}% */

QMainWindow, QDialog, QWidget {{
    background-color: {p['bg']}; color: {p['text']};
    font-family: 'Segoe UI Variable Display', 'Segoe UI', 'SF Pro Display', system-ui, sans-serif;
    font-size: {px(13)}px;
}}
QDialog QLabel {{ color: {p['text']}; }}
QDialog QLineEdit {{
    background-color: {p['bg_tooltip']}; color: {p['text']};
    border: 1px solid {p['border_light']}; border-radius: {px(5)}px;
    padding: {px(7)}px {px(10)}px;
}}
QDialog QPushButton {{ min-width: {px(80)}px; }}

QTabWidget::pane {{ border: none; background: {p['bg']}; border-top: 1px solid {p['border']}; }}
QTabBar::tab {{
    background: transparent; color: {p['text_muted']};
    padding: {px(10)}px {px(24)}px; border: none;
    border-bottom: 2px solid transparent;
    font-weight: 500; font-size: {px(12)}px;
}}
QTabBar::tab:selected {{ color: {p['text']}; border-bottom: 2px solid {p['accent']}; }}
QTabBar::tab:hover:!selected {{ color: {p['text_hover']}; background: {p['bg_card']}; }}

QPushButton {{
    background-color: {p['accent']}; color: #ffffff; border: none;
    padding: {px(8)}px {px(18)}px; border-radius: {px(5)}px;
    font-weight: 600; font-size: {px(12)}px;
}}
QPushButton:hover {{ background-color: {p['accent_hover']}; }}
QPushButton:pressed {{ background-color: {p['accent_pressed']}; }}
QPushButton:disabled {{ background-color: {p['bg_button']}; color: {p['text_disabled']}; }}
QPushButton:checked {{ background-color: {p['accent_pressed']}; color: {p['text']}; border: 1px solid {p['accent']}; }}
QPushButton#danger  {{ background-color: {p['danger']}; color: #ffffff; }}
QPushButton#danger:hover  {{ background-color: {p['danger_hover']}; }}
QPushButton#success {{ background-color: {p['success_btn']}; color: #ffffff; }}
QPushButton#success:hover {{ background-color: {p['success_btn_h']}; }}
QPushButton#warning {{ background-color: {p['warning_btn']}; color: #ffffff; }}
QPushButton#warning:hover {{ background-color: {p['warning_btn_h']}; }}
QPushButton#neutral {{ background-color: {p['bg_button']}; color: {p['text_neutral']}; }}
QPushButton#neutral:hover {{ background-color: {p['bg_hover']}; color: {p['text']}; }}
QPushButton#neutral:checked {{ background-color: {p['accent_bg']}; border: 1px solid {p['accent']}; color: {p['accent']}; }}

QLineEdit, QSpinBox, QComboBox, QDoubleSpinBox {{
    background-color: {p['bg_input']}; color: {p['text']};
    border: 1px solid {p['border_input']}; border-radius: {px(5)}px;
    padding: {px(6)}px {px(10)}px;
    selection-background-color: {p['accent']}; selection-color: #ffffff;
    font-size: {px(13)}px;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {p['accent']}; }}
QSpinBox::up-button, QSpinBox::down-button {{
    background: {p['bg_button']}; border: none; border-radius: 2px; width: {px(20)}px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {p['border_light']}; }}
QSpinBox::up-arrow {{
    image: none;
    border-left: {px(4)}px solid transparent; border-right: {px(4)}px solid transparent;
    border-bottom: {px(5)}px solid {p['text_hover']};
}}
QSpinBox::down-arrow {{
    image: none;
    border-left: {px(4)}px solid transparent; border-right: {px(4)}px solid transparent;
    border-top: {px(5)}px solid {p['text_hover']};
}}
QComboBox::drop-down {{ border: none; width: {px(26)}px; subcontrol-position: right center; }}
QComboBox::down-arrow {{
    image: none;
    border-left: {px(4)}px solid transparent; border-right: {px(4)}px solid transparent;
    border-top: {px(5)}px solid {p['text_muted']};
}}
QComboBox QAbstractItemView {{
    background-color: {p['bg_input']}; color: {p['text']};
    border: 1px solid {p['border_light']}; selection-background-color: {p['accent_bg']};
    selection-color: {p['accent_hover']}; padding: {px(4)}px; outline: none;
}}
QComboBox QAbstractItemView::item {{ padding: {px(6)}px {px(10)}px; border-radius: {px(3)}px; }}
QComboBox QAbstractItemView::item:selected {{ background-color: {p['accent_bg']}; }}

QTextEdit, QPlainTextEdit {{
    background-color: {p['bg_deep']}; color: {p['log_text']};
    border: 1px solid {p['border']}; border-radius: {px(5)}px;
    padding: {px(8)}px;
    font-family: 'Cascadia Code', 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
    font-size: {px(12)}px;
}}

QGroupBox {{
    border: 1px solid {p['border']}; border-radius: {px(8)}px;
    margin-top: {px(14)}px;
    padding: {px(16)}px {px(14)}px {px(12)}px {px(14)}px;
    color: {p['text']}; font-weight: 600; font-size: {px(13)}px;
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: {px(14)}px;
    padding: 0 {px(8)}px; color: {p['accent_hover']}; font-weight: 600;
}}
QCheckBox {{ color: {p['text']}; spacing: {px(6)}px; font-size: {px(13)}px; }}
QCheckBox::indicator {{
    width: {px(16)}px; height: {px(16)}px;
    border-radius: {px(3)}px; border: 1px solid {p['border_light']}; background: {p['bg_input']};
}}
QCheckBox::indicator:checked {{ background-color: {p['accent']}; border-color: {p['accent']}; }}
QCheckBox::indicator:hover {{ border-color: {p['accent']}; }}

QLabel {{ color: {p['text']}; background: transparent; font-size: {px(13)}px; }}
QLabel#subtext {{ color: {p['text_muted']}; font-size: {px(11)}px; }}

QProgressBar {{
    background-color: {p['bg_card']}; border: none; border-radius: {px(3)}px;
    text-align: center; color: {p['text']}; height: {px(6)}px;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {p['accent']}, stop:0.5 {p['accent_hover']}, stop:1 {p['accent_grad']});
    border-radius: {px(3)}px;
}}

QTableWidget {{
    background-color: {p['bg_deep']}; alternate-background-color: {p['bg']};
    color: {p['text']}; border: 1px solid {p['border']};
    gridline-color: {p['bg_card']}; font-size: {px(12)}px;
}}
QTableWidget::item {{ padding: {px(4)}px {px(8)}px; }}
QTableWidget::item:selected {{ background-color: {p['accent_bg']}; color: {p['accent_hover']}; }}
QHeaderView::section {{
    background-color: {p['bg_card_area']}; color: {p['text_muted']}; border: none;
    border-right: 1px solid {p['border_subtle']}; border-bottom: 1px solid {p['border']};
    padding: {px(7)}px {px(10)}px; font-weight: 600;
    font-size: {px(11)}px; text-transform: uppercase; letter-spacing: 0.6px;
}}

QScrollBar:vertical {{
    background: transparent; width: {px(7)}px; border: none; margin: {px(3)}px 1px;
}}
QScrollBar::handle:vertical {{
    background: {p['border_light']}; border-radius: {px(3)}px; min-height: {px(40)}px;
}}
QScrollBar::handle:vertical:hover {{ background: {p['text_disabled']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: transparent; height: {px(7)}px; border: none; margin: 1px {px(3)}px;
}}
QScrollBar::handle:horizontal {{
    background: {p['border_light']}; border-radius: {px(3)}px; min-width: {px(40)}px;
}}
QScrollBar::handle:horizontal:hover {{ background: {p['text_disabled']}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

QStatusBar {{
    background-color: {p['bg_deep']}; color: {p['text_muted']};
    border-top: 1px solid {p['border_subtle']}; padding: 0 {px(14)}px;
    font-size: {px(12)}px;
}}

QFrame#stat-card {{
    background-color: {p['bg_card']}; border: 1px solid {p['border']}; border-radius: {px(8)}px;
}}
QFrame#clip-card {{
    background-color: {p['bg_card']}; border: 1px solid {p['border_card']}; border-radius: {px(8)}px;
}}
QFrame#clip-card:hover {{ border-color: {p['accent']}44; background-color: {p['bg_hover']}; }}
QPushButton#tag-chip {{
    background: {p['bg_button']}; color: {p['purple']};
    font-size: {px(10)}px; padding: {px(1)}px {px(6)}px;
    border-radius: {px(3)}px; font-weight: 600; border: none; text-align: left;
}}
QPushButton#tag-chip:hover {{ background: {p['bg_hover']}; color: {p['purple_hover']}; }}

QSlider::groove:horizontal {{ background: {p['bg_button']}; height: {px(3)}px; border-radius: 1px; }}
QSlider::handle:horizontal {{
    background: {p['accent']}; width: {px(14)}px; height: {px(14)}px;
    margin: {px(-6)}px 0; border-radius: {px(7)}px;
}}
QSlider::handle:horizontal:hover {{ background: {p['accent_hover']}; }}
QSlider::sub-page:horizontal {{ background: {p['accent']}; border-radius: 1px; }}

QMenu {{
    background: {p['bg_input']}; color: {p['text']};
    border: 1px solid {p['border_input']}; padding: {px(5)}px; border-radius: {px(8)}px;
}}
QMenu::item {{
    padding: {px(7)}px {px(28)}px {px(7)}px {px(16)}px;
    border-radius: {px(4)}px; font-size: {px(12)}px;
}}
QMenu::item:selected {{ background: {p['accent_bg']}; color: {p['accent_hover']}; }}
QMenu::item:disabled {{ color: {p['text_disabled']}; }}
QMenu::separator {{ height: 1px; background: {p['border']}; margin: {px(4)}px {px(8)}px; }}
QMenu::right-arrow {{ width: {px(12)}px; height: {px(12)}px; }}

QToolTip {{
    background: {p['bg_tooltip']}; color: {p['text']}; border: 1px solid {p['border_light']};
    padding: {px(6)}px {px(10)}px; border-radius: {px(5)}px;
    font-size: {px(12)}px;
}}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QFrame[frameShape="4"], QFrame[frameShape="5"] {{ color: {p['border']}; }}
QSplitter::handle {{ background: {p['border']}; width: 1px; }}
"""

# Default stylesheet for initial load
DARK_STYLE = _build_stylesheet(1.0)

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
                color       TEXT DEFAULT '#3d8af7',
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
        # ── FTS integrity check — auto-rebuild if corrupted ───────────
        self._check_fts_health()

    def _check_fts_health(self):
        """Startup check: verify FTS table is readable. Auto-rebuild if corrupted."""
        try:
            self.conn.execute("SELECT COUNT(*) FROM clips_fts").fetchone()
        except Exception as e:
            err_s = str(e).lower()
            if 'malformed' in err_s or 'corrupt' in err_s or 'no such table' in err_s:
                print(f"[DB] FTS corruption detected at startup: {e}")
                print("[DB] Running automatic FTS rebuild...")
                try:
                    self.conn.execute("DROP TABLE IF EXISTS clips_fts")
                    self.conn.execute("""
                        CREATE VIRTUAL TABLE clips_fts USING fts5(
                            title, creator, collection, tags, resolution, camera, duration,
                            content='clips', content_rowid='id',
                            tokenize='porter unicode61'
                        )
                    """)
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
                    print(f"[DB] Startup FTS rebuild complete: {count} rows indexed")
                except Exception as rebuild_err:
                    print(f"[DB ERROR] Startup FTS rebuild failed: {rebuild_err}")

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
                self.conn.commit()
                # FTS indexing — separate try so main insert succeeds even if FTS corrupted
                if is_new:
                    rowid = cur.lastrowid
                    try:
                        self.conn.execute("""
                            INSERT INTO clips_fts(rowid,title,creator,collection,tags,resolution,camera,duration)
                            VALUES (?,?,?,?,?,?,?,?)
                        """, (rowid,
                              data.get('title',''), data.get('creator',''),
                              data.get('collection',''), data.get('tags',''),
                              data.get('resolution',''), data.get('camera',''),
                              data.get('duration','')))
                        self.conn.commit()
                    except Exception as fts_err:
                        err_s = str(fts_err).lower()
                        if 'malformed' in err_s or 'corrupt' in err_s:
                            self._fts_recover()
                        else:
                            print(f"[DB WARN] FTS insert failed for {data.get('clip_id','?')}: {fts_err}")
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
                self.conn.commit()
        except Exception as e:
            print(f"[DB WARN] update_metadata UPDATE failed for {clip_id}: {e}")
            return
        # Re-index FTS separately — auto-recovers on corruption
        self._fts_safe_reindex(clip_id)

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
        except Exception as e:
            err_s = str(e).lower()
            if 'malformed' in err_s or 'corrupt' in err_s:
                self._fts_recover()
            # Fallback: plain query without FTS
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
        'user_rating','user_tags','favorited','source_site',
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

    def wal_checkpoint(self):
        """Truncate the WAL file to prevent unbounded growth during long sessions."""
        try:
            with self._lock:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

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
                self.conn.commit()
        except Exception as e:
            print(f"[DB WARN] set_user_tags UPDATE failed for {clip_id}: {e}")
            return
        # Re-index FTS separately — auto-recovers on corruption
        self._fts_safe_reindex(clip_id)

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

    def create_collection(self, name, color='#3d8af7'):
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
                      min_rating=0, sort_by='', limit=3000, offset=0):
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
        _SORT_MAP = {
            'newest':      'c.found_at DESC',
            'oldest':      'c.found_at ASC',
            'title_az':    'c.title ASC',
            'title_za':    'c.title DESC',
            'resolution':  "CAST(REPLACE(SUBSTR(c.resolution, INSTR(c.resolution,'x')+1),' ','') AS INTEGER) DESC",
            'duration_short': """CASE WHEN c.duration LIKE '%:%' THEN
                CAST(SUBSTR(c.duration,1,INSTR(c.duration,':')-1) AS REAL)*60 +
                CAST(SUBSTR(c.duration,INSTR(c.duration,':')+1) AS REAL)
                ELSE 0 END ASC""",
            'duration_long': """CASE WHEN c.duration LIKE '%:%' THEN
                CAST(SUBSTR(c.duration,1,INSTR(c.duration,':')-1) AS REAL)*60 +
                CAST(SUBSTR(c.duration,INSTR(c.duration,':')+1) AS REAL)
                ELSE 0 END DESC""",
            'rating':      'c.user_rating DESC, c.found_at DESC',
        }
        if sort_by and sort_by in _SORT_MAP:
            base += f" ORDER BY {_SORT_MAP[sort_by]}"
        elif query and query.strip() and not collection_id:
            base += " ORDER BY rank"
        else:
            base += " ORDER BY c.found_at DESC"
        base += " LIMIT ? OFFSET ?"
        params += [limit, offset]

        try:
            with self._lock:
                return self.conn.execute(base, params).fetchall()
        except Exception as e:
            err_s = str(e).lower()
            if 'malformed' in err_s or 'corrupt' in err_s:
                self._fts_recover()
            # Fallback
            with self._lock:
                return self.conn.execute(
                    "SELECT * FROM clips ORDER BY found_at DESC LIMIT ? OFFSET ?",
                    (limit, offset)).fetchall()

    def clear_all(self):
        with self._lock:
            try:
                self.conn.executescript("""
                    DELETE FROM clips;
                    DELETE FROM crawled_pages; DELETE FROM crawl_queue;
                    DELETE FROM clip_collections; DELETE FROM collections;
                    DELETE FROM saved_searches;
                """)
            except Exception as e:
                print(f"[DB WARN] clear_all partial failure: {e}")
            # FTS: DROP+recreate is safest (handles corruption)
            try:
                self.conn.execute("DROP TABLE IF EXISTS clips_fts")
                self.conn.execute("""
                    CREATE VIRTUAL TABLE clips_fts USING fts5(
                        title, creator, collection, tags, resolution, camera, duration,
                        content='clips', content_rowid='id',
                        tokenize='porter unicode61'
                    )
                """)
            except Exception as e:
                print(f"[DB WARN] clear_all FTS recreate failed: {e}")
            self.conn.commit()

    def rebuild_fts(self):
        """Nuclear FTS rebuild — DROP + recreate + repopulate.
        Handles corruption where even DELETE FROM clips_fts fails."""
        try:
            with self._lock:
                # Nuclear: DROP corrupted table entirely, then recreate
                self.conn.execute("DROP TABLE IF EXISTS clips_fts")
                self.conn.execute("""
                    CREATE VIRTUAL TABLE clips_fts USING fts5(
                        title, creator, collection, tags, resolution, camera, duration,
                        content='clips', content_rowid='id',
                        tokenize='porter unicode61'
                    )
                """)
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
                print(f"[DB] FTS index rebuilt (DROP+recreate): {count} rows indexed")
                return count
        except Exception as e:
            print(f"[DB ERROR] FTS rebuild failed: {e}")
            return -1

    _fts_recovering = False  # prevent recursive recovery

    def _fts_recover(self):
        """Auto-recover from FTS corruption. Called when 'disk image is malformed' detected."""
        if self._fts_recovering:
            return False
        self._fts_recovering = True
        print("[DB] FTS corruption detected — running automatic recovery...")
        try:
            result = self.rebuild_fts()
            self._fts_recovering = False
            return result >= 0
        except Exception as e:
            self._fts_recovering = False
            print(f"[DB ERROR] FTS auto-recovery failed: {e}")
            return False

    def _fts_safe_reindex(self, clip_id):
        """Re-index a single clip in FTS with auto-recovery on corruption."""
        try:
            with self._lock:
                row = self.conn.execute(
                    "SELECT id,title,creator,collection,tags,resolution,camera,duration,user_tags "
                    "FROM clips WHERE clip_id=?",
                    (clip_id,)).fetchone()
                if not row:
                    return
                try:
                    self.conn.execute("DELETE FROM clips_fts WHERE rowid=?", (row['id'],))
                except Exception:
                    pass  # Row may not exist, or table may be corrupted — handled below
                all_tags = ', '.join(filter(None, [row['tags'] or '', row['user_tags'] or '']))
                self.conn.execute("""
                    INSERT INTO clips_fts(rowid,title,creator,collection,tags,resolution,camera,duration)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (row['id'], row['title'] or '', row['creator'] or '',
                      row['collection'] or '', all_tags,
                      row['resolution'] or '', row['camera'] or '', row['duration'] or ''))
                self.conn.commit()
        except Exception as e:
            err = str(e).lower()
            if 'malformed' in err or 'corrupt' in err or 'fts' in err:
                self._fts_recover()
            else:
                print(f"[DB WARN] _fts_safe_reindex failed for {clip_id}: {e}")

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
        # Catalog card extraction: JS that returns [{clip_id, title, creator, duration, thumbnail_url, source_url}]
        self.catalog_card_js    = kw.get('catalog_card_js', '')

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
        'resolution': r'Resolution[\s\n]+([\d]{3,5}\s*[xX\u00d7]\s*[\d]{3,5})',
        'duration':   r'Length[\s\n]+([\d:]{4,8})',
        'frame_rate': r'Frame\s+Rate[\s\n]+(\d+)',
        'camera':     r'Camera[\s\n]+([^\n\r]{2,50}?)(?:\n|\r|Available)',
        'formats':    r'Available\s+Formats[\s\n]+((?:(?:HD|SD|4K|2K|ProRes|MP4|MOV|RAW)\s*)+)',
        'creator':    r'Clip by\s*\n?\s*([^\n\r]{2,50})',
        'collection': r'Part of\s*\n?\s*([^\n\r]{2,60})',
        'tags':       r'Tags\s*\n((?:.+\n?){1,25}?)(?:Related|Part of|Clip by|Similar|Explore|$)',
    },
    og_fallback=True,
    catalog_card_js="""
    (() => {
        const clips = [];
        const seen = new Set();

        // ── Strategy 1: __NEXT_DATA__ (Next.js server-side props) ──
        try {
            const nd = document.getElementById('__NEXT_DATA__');
            if (nd) {
                const data = JSON.parse(nd.textContent);
                const walk = (obj) => {
                    if (!obj || typeof obj !== 'object') return;
                    if (Array.isArray(obj)) { obj.forEach(walk); return; }
                    // Look for clip-like objects with numeric IDs
                    const id = String(obj.id || obj.clipId || obj.clip_id || '');
                    if (id && /^\\d{4,}$/.test(id) && !seen.has(id)) {
                        seen.add(id);
                        clips.push({
                            clip_id: id,
                            title: obj.title || obj.name || obj.clipTitle || '',
                            creator: obj.artistName || obj.artist?.name || obj.creatorName || obj.creator || '',
                            duration: obj.duration || obj.length || '',
                            thumbnail_url: obj.thumbnailUrl || obj.thumbnail || obj.imageUrl || obj.image?.url || obj.posterUrl || '',
                            resolution: obj.resolution || '',
                            tags: Array.isArray(obj.tags) ? obj.tags.map(t => typeof t === 'string' ? t : t.name || '').join(', ') : (obj.tags || ''),
                            collection: obj.collectionName || obj.collection?.name || obj.folderName || '',
                            source_url: obj.url || obj.pageUrl || (id ? '/stock-footage/clip/' + id : ''),
                            m3u8_url: obj.videoUrl || obj.hlsUrl || obj.m3u8Url || obj.previewUrl || '',
                            frame_rate: obj.fps || obj.frameRate || '',
                            camera: obj.camera || obj.cameraModel || '',
                            formats: obj.formats || '',
                        });
                    }
                    Object.values(obj).forEach(walk);
                };
                walk(data);
            }
        } catch(e) {}

        // ── Strategy 2: DOM card parsing ──
        // Artlist cards: <a href="/stock-footage/..."> wrapping img + text
        try {
            const cards = document.querySelectorAll('a[href*="/stock-footage/"][href$="/"]' +
                ', a[href*="/stock-footage/"][href*="/"]');
            cards.forEach(card => {
                const href = card.href || card.getAttribute('href') || '';
                const idM = href.match(/\\/(\\d{4,})\\/?$/);
                if (!idM) return;
                const id = idM[1];
                if (seen.has(id)) return;
                seen.add(id);

                // Find thumbnail
                const img = card.querySelector('img[src], img[data-src], img[srcset]');
                let thumb = '';
                if (img) {
                    thumb = img.src || img.dataset.src || '';
                    if (!thumb && img.srcset) {
                        const parts = img.srcset.split(',').map(s => s.trim().split(' ')[0]);
                        thumb = parts[parts.length - 1] || '';
                    }
                }
                // Also check picture > source
                if (!thumb) {
                    const source = card.querySelector('picture source[srcset]');
                    if (source) {
                        const parts = source.srcset.split(',').map(s => s.trim().split(' ')[0]);
                        thumb = parts[parts.length - 1] || '';
                    }
                }
                // Background image fallback
                if (!thumb) {
                    const bgEl = card.querySelector('[style*="background-image"]');
                    if (bgEl) {
                        const bgM = bgEl.style.backgroundImage.match(/url\\(['"]?([^'"\\)]+)/);
                        if (bgM) thumb = bgM[1];
                    }
                }

                // Find text content
                const allText = card.innerText || '';
                const lines = allText.split('\\n').map(l => l.trim()).filter(l => l);

                // Duration: look for MM:SS or H:MM:SS pattern
                let duration = '';
                const durEl = card.querySelector('[class*="uration"], [class*="time"], [class*="length"]');
                if (durEl) duration = durEl.innerText.trim();
                if (!duration) {
                    for (const l of lines) {
                        if (/^\\d{1,2}:\\d{2}(:\\d{2})?$/.test(l)) { duration = l; break; }
                    }
                }

                // Title: first substantial text line that isn't duration
                let title = '';
                for (const l of lines) {
                    if (l === duration) continue;
                    if (l.length > 3 && l.length < 200 && !/^\\d{1,2}:\\d{2}/.test(l)) {
                        title = l; break;
                    }
                }

                // Creator: second text line or element with "by" prefix
                let creator = '';
                const byEl = card.querySelector('[class*="rtist"], [class*="reator"], [class*="author"]');
                if (byEl) creator = byEl.innerText.trim().replace(/^by\\s+/i, '');

                clips.push({
                    clip_id: id, title, creator, duration, thumbnail_url: thumb,
                    source_url: href.startsWith('http') ? href : location.origin + href,
                    resolution: '', tags: '', collection: '', m3u8_url: '',
                    frame_rate: '', camera: '', formats: '',
                });
            });
        } catch(e) {}

        // ── Strategy 3: video elements with poster attributes ──
        try {
            document.querySelectorAll('video[poster]').forEach(v => {
                const poster = v.poster || '';
                const link = v.closest('a[href*="/stock-footage/"]');
                if (!link) return;
                const idM = link.href.match(/\\/(\\d{4,})\\/?$/);
                if (!idM || seen.has(idM[1])) return;
                seen.add(idM[1]);
                clips.push({
                    clip_id: idM[1], title: '', creator: '', duration: '',
                    thumbnail_url: poster,
                    source_url: link.href.startsWith('http') ? link.href : location.origin + link.href,
                    resolution: '', tags: '', collection: '', m3u8_url: '',
                    frame_rate: '', camera: '', formats: '',
                });
            });
        } catch(e) {}

        return clips;
    })()
    """,
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

            crawl_mode = self.cfg.get('crawl_mode', 'full')
            self.log(f"Crawl mode: {crawl_mode}", "INFO")

            # ── Seed based on mode ────────────────────────────────────────
            if crawl_mode == 'm3u8_only':
                # Seed from DB: clips that have metadata but no M3U8 URL
                missing = self.db.execute("""
                    SELECT clip_id, source_url FROM clips
                    WHERE (m3u8_url IS NULL OR m3u8_url = '')
                    AND source_url != '' AND clip_id != ''
                    ORDER BY id DESC
                """).fetchall()
                seeded = 0
                for row in missing:
                    src_url = row['source_url']
                    if src_url and src_url.startswith('http'):
                        # Determine which profile this clip belongs to
                        prof_name = 'Artlist'  # default
                        for _p in self._profiles:
                            if any(d in src_url for d in _p.domains):
                                prof_name = _p.name; break
                        self.db.enqueue(src_url, 0, 10, profile=prof_name)
                        seeded += 1
                self.log(f"M3U8 Harvest: seeded {seeded} clips missing M3U8 URLs", "OK")
            else:
                # Normal seeding: start URLs from profiles
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

                        # ── Crawl Mode dispatch ──────────────────────────
                        if crawl_mode == 'catalog_sweep':
                            if is_clip:
                                # Don't visit clip pages in catalog sweep mode
                                # Just mark processed — metadata already extracted from cards
                                self.db.mark_processed(url, depth)
                                continue
                            elif is_cat:
                                self.log(f"[{pname}] CATALOG [d{depth}] p{page_count} {url[:80]}", "INFO")
                                await self._crawl_catalog(page, url, depth)
                            else:
                                # Generic page — treat as catalog (might have cards)
                                self.log(f"[{pname}] GENERIC->CATALOG [d{depth}] p{page_count} {url[:80]}", "INFO")
                                await self._crawl_catalog(page, url, depth)
                        elif crawl_mode == 'm3u8_only':
                            if is_clip or not is_cat:
                                self.log(f"[{pname}] M3U8 HARVEST [d{depth}] p{page_count} {url[:80]}", "INFO")
                                await self._crawl_clip(context, url, depth)
                            else:
                                self.db.mark_processed(url, depth)
                                continue
                        else:
                            # Full mode — original behavior
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
                if not vid_m and clip_meta.get('m3u8_url'):
                    return  # Can't verify + already have a video URL — skip related preview

            # ── M3U8 master playlist: extract resolution from RESOLUTION= ──
            if '.m3u8' in url.lower():
                try:
                    body = await response.text()
                    resolutions = re.findall(r'RESOLUTION=(\d{3,5})x(\d{3,5})', body)
                    if resolutions:
                        # Pick highest resolution variant
                        best_w, best_h = max(resolutions, key=lambda r: int(r[0]) * int(r[1]))
                        if not clip_meta.get('resolution'):
                            clip_meta['resolution'] = f"{best_w}x{best_h}"
                        # Also extract frame rate if available
                        fps_m = re.search(r'FRAME-RATE=([\d.]+)', body)
                        if fps_m and not clip_meta.get('frame_rate'):
                            clip_meta['frame_rate'] = fps_m.group(1).split('.')[0]
                except Exception:
                    pass  # Non-text response or read error

            await self._record_video_url(url.strip(), source_url, clip_meta)
            # Mark that we have a video URL so subsequent unverifiable responses are skipped
            if not clip_meta.get('m3u8_url'):
                clip_meta['m3u8_url'] = url.strip()
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
        elif meta.get('resolution'):
            # Resolution from M3U8 master playlist or metadata extraction
            res_parts = re.match(r'(\d+)x(\d+)', meta['resolution'])
            if res_parts:
                quality_label = f"{max(int(res_parts.group(1)), int(res_parts.group(2)))}p"
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
            # ── Hook API response interception for bulk clip data ──────
            async def _cat_resp_handler(resp):
                try:
                    await self._on_catalog_response(resp, url)
                except Exception:
                    pass
            page.on('response', _cat_resp_handler)

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

            # ── Bulk metadata extraction from card grid ───────────────────
            card_count = await self._extract_catalog_cards(page, url)

            # ── Infinite scroll loop for catalog sweep ────────────────────
            # Keep scrolling + extracting until no new cards appear
            crawl_mode = self.cfg.get('crawl_mode', 'full')
            if crawl_mode == 'catalog_sweep':
                max_scroll_rounds = 50  # safety cap
                for scroll_round in range(max_scroll_rounds):
                    if self._stop.is_set():
                        break
                    # Get current page height
                    prev_height = await page.evaluate("document.body.scrollHeight")
                    # Scroll to bottom with randomized behavior
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                    new_height = await page.evaluate("document.body.scrollHeight")
                    if new_height <= prev_height:
                        # No new content loaded — try one more time
                        await asyncio.sleep(2.0)
                        new_height = await page.evaluate("document.body.scrollHeight")
                        if new_height <= prev_height:
                            self.log(f"  [scroll] No more content after {scroll_round+1} rounds", "INFO")
                            break
                    # Extract any newly loaded cards
                    new_cards = await self._extract_catalog_cards(page, url)
                    card_count += new_cards
                    self.log(
                        f"  [scroll] Round {scroll_round+1}: +{new_cards} cards (total: {card_count})",
                        "INFO" if new_cards else "DEBUG")
                    if new_cards == 0:
                        # No new cards even though page grew — might be footer/ads
                        break

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

            # Unhook catalog response handler
            try:
                page.remove_listener('response', _cat_resp_handler)
            except Exception:
                pass

            self.db.mark_processed(url, depth)
            self.log(
                f"CATALOG done — {card_count} cards extracted, {queued} items queued  (depth {depth})",
                "OK")
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
                    elif item.get('href'):
                        # Fallback: extract clip ID from linked clip page URL
                        href_id_m = re.search(r'/(\d{4,})(?:/|$)', item['href'])
                        if href_id_m:
                            meta['clip_id'] = href_id_m.group(1)
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

    # ── Bulk catalog card metadata extraction ─────────────────────────────

    async def _extract_catalog_cards(self, page, source_url):
        """
        Bulk-extract clip metadata from catalog page cards.
        Uses profile's catalog_card_js (if set) or generic card parsing.
        Returns count of new/updated clips.
        """
        new_count = 0
        updated_count = 0

        # ── Strategy 1: Profile-specific JS card extractor ────────────
        cards = []
        if self.profile.catalog_card_js:
            try:
                cards = await page.evaluate(self.profile.catalog_card_js) or []
                self.log(f"  [catalog-cards] Profile JS extracted {len(cards)} cards", "INFO")
            except Exception as e:
                self.log(f"  [catalog-cards] Profile JS error: {str(e)[:80]}", "WARN")

        # ── Strategy 2: Generic — intercept __NEXT_DATA__ ─────────────
        if not cards:
            try:
                cards = await page.evaluate("""
                    (() => {
                        const clips = [];
                        const seen = new Set();
                        // Try __NEXT_DATA__
                        const nd = document.getElementById('__NEXT_DATA__');
                        if (nd) {
                            const walk = (obj) => {
                                if (!obj || typeof obj !== 'object') return;
                                if (Array.isArray(obj)) { obj.forEach(walk); return; }
                                const id = String(obj.id || obj.clipId || obj.clip_id || '');
                                if (id && /^\\d{4,}$/.test(id) && !seen.has(id)) {
                                    seen.add(id);
                                    clips.push({
                                        clip_id: id,
                                        title: obj.title || obj.name || '',
                                        creator: obj.artistName || obj.creatorName || '',
                                        duration: obj.duration || '',
                                        thumbnail_url: obj.thumbnailUrl || obj.thumbnail || obj.imageUrl || '',
                                        source_url: obj.url || '',
                                    });
                                }
                                Object.values(obj).forEach(walk);
                            };
                            try { walk(JSON.parse(nd.textContent)); } catch(e) {}
                        }
                        return clips;
                    })()
                """) or []
                if cards:
                    self.log(f"  [catalog-cards] __NEXT_DATA__ extracted {len(cards)} cards", "INFO")
            except Exception:
                pass

        # ── Strategy 3: Generic DOM card parsing ──────────────────────
        if not cards:
            try:
                cards = await page.evaluate("""
                    (() => {
                        const clips = [];
                        const seen = new Set();
                        // Find all links with numeric IDs in the path
                        document.querySelectorAll('a[href]').forEach(a => {
                            const href = a.href || '';
                            const m = href.match(/\\/(\\d{4,})\\/?$/);
                            if (!m || seen.has(m[1])) return;
                            // Must have visual content (image or video)
                            const img = a.querySelector('img[src], img[data-src], video[poster]');
                            if (!img) return;
                            seen.add(m[1]);
                            let thumb = img.src || img.dataset.src || img.poster || '';
                            clips.push({
                                clip_id: m[1],
                                title: (a.getAttribute('aria-label') || a.getAttribute('title') || img.alt || '').trim(),
                                creator: '',
                                duration: '',
                                thumbnail_url: thumb,
                                source_url: href.startsWith('http') ? href : location.origin + href,
                            });
                        });
                        return clips;
                    })()
                """) or []
                if cards:
                    self.log(f"  [catalog-cards] Generic DOM extracted {len(cards)} cards", "INFO")
            except Exception:
                pass

        if not cards:
            self.log(f"  [catalog-cards] No cards extracted from catalog page", "DEBUG")
            return 0

        # ── Save extracted cards to DB ────────────────────────────────
        thumb_dir = os.path.join(
            os.environ.get('APPDATA', os.path.expanduser('~')),
            'ArtlistScraper', 'thumbnails')
        os.makedirs(thumb_dir, exist_ok=True)

        for card in cards:
            if self._stop.is_set():
                break
            clip_id = str(card.get('clip_id', '') or '').strip()
            if not clip_id:
                continue

            meta = {k: '' for k in ('clip_id','source_url','title','creator','collection',
                                     'resolution','duration','frame_rate','camera',
                                     'formats','tags','thumbnail_url','m3u8_url','source_site')}
            meta['clip_id'] = clip_id
            meta['source_site'] = self.profile.name
            # Fill in whatever the card extraction gave us
            for field in ('title','creator','duration','thumbnail_url','source_url',
                          'resolution','tags','collection','m3u8_url','frame_rate','camera','formats'):
                v = str(card.get(field, '') or '').strip()
                if v:
                    meta[field] = v

            # Ensure source_url is absolute
            if meta['source_url'] and not meta['source_url'].startswith('http'):
                base = urlparse(source_url)
                meta['source_url'] = f"{base.scheme}://{base.netloc}{meta['source_url']}"

            is_new = self.db.save_clip(meta)
            if is_new:
                new_count += 1
            else:
                # Backfill empty fields on existing record
                self.db.update_metadata(clip_id, meta)
                updated_count += 1

            # Download thumbnail if we have a URL and no local thumb yet
            thumb_url = meta.get('thumbnail_url', '')
            if thumb_url:
                thumb_path = os.path.join(thumb_dir, f"{clip_id}.jpg")
                if not os.path.isfile(thumb_path) or os.path.getsize(thumb_path) == 0:
                    self._download_thumb_url(thumb_url, thumb_path, clip_id)

        self.log(
            f"  [catalog-cards] Saved {new_count} new + {updated_count} updated clips "
            f"({len(cards)} total cards)",
            "OK" if new_count else "INFO")
        self.stats_signal.emit(self.db.stats())
        return new_count + updated_count

    def _download_thumb_url(self, url, out_path, clip_id):
        """Download a thumbnail URL to disk. Non-blocking (runs in crawl thread)."""
        try:
            import urllib.request
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'Mozilla/5.0')
            resp = urllib.request.urlopen(req, timeout=10)
            data = resp.read(2_000_000)  # cap at 2MB
            resp.close()
            if len(data) > 500:
                with open(out_path, 'wb') as f:
                    f.write(data)
                self.db.update_thumb_path(clip_id, out_path)
        except Exception:
            pass  # Thumbnail download failure is non-critical

    # ── Catalog API response interception ─────────────────────────────────

    async def _on_catalog_response(self, response, source_url):
        """Intercept JSON API responses on catalog pages for bulk clip data."""
        try:
            url = response.url
            ct = response.headers.get('content-type', '')
            if 'json' not in ct:
                return
            rt = response.request.resource_type
            if rt not in ('xhr', 'fetch'):
                return
            # Only process reasonably-sized JSON (skip huge payloads)
            cl = int(response.headers.get('content-length', '0') or 0)
            if cl > 5_000_000 or cl == 0:
                pass  # Unknown size, try anyway (but cap read)

            body = await response.text()
            if len(body) < 50 or len(body) > 5_000_000:
                return

            data = json.loads(body)

            # Walk the JSON for clip-like objects
            clips_found = 0
            thumb_dir = os.path.join(
                os.environ.get('APPDATA', os.path.expanduser('~')),
                'ArtlistScraper', 'thumbnails')

            def _walk(obj):
                nonlocal clips_found
                if isinstance(obj, list):
                    for item in obj:
                        _walk(item)
                    return
                if not isinstance(obj, dict):
                    return
                # Check if this looks like a clip object
                cid = str(obj.get('id', '') or obj.get('clipId', '') or obj.get('clip_id', '') or '')
                if cid and re.match(r'^\d{4,}$', cid):
                    meta = {k: '' for k in ('clip_id','source_url','title','creator','collection',
                                             'resolution','duration','frame_rate','camera',
                                             'formats','tags','thumbnail_url','m3u8_url','source_site')}
                    meta['clip_id'] = cid
                    meta['source_site'] = self.profile.name
                    meta['title'] = str(obj.get('title', '') or obj.get('name', '') or '')
                    meta['creator'] = str(obj.get('artistName', '') or obj.get('artist', {}).get('name', '') if isinstance(obj.get('artist'), dict) else obj.get('artist', '') or obj.get('creatorName', '') or '')
                    meta['duration'] = str(obj.get('duration', '') or obj.get('length', '') or '')
                    meta['thumbnail_url'] = str(obj.get('thumbnailUrl', '') or obj.get('thumbnail', '') or obj.get('imageUrl', '') or obj.get('posterUrl', '') or '')
                    meta['resolution'] = str(obj.get('resolution', '') or '')
                    meta['frame_rate'] = str(obj.get('fps', '') or obj.get('frameRate', '') or '')
                    meta['camera'] = str(obj.get('camera', '') or obj.get('cameraModel', '') or '')
                    meta['collection'] = str(obj.get('collectionName', '') or '')

                    # Tags
                    tags = obj.get('tags', '')
                    if isinstance(tags, list):
                        tags = ', '.join(str(t.get('name', '') if isinstance(t, dict) else t) for t in tags[:25])
                    meta['tags'] = str(tags or '')

                    # Video URL
                    for vk in ('videoUrl', 'hlsUrl', 'm3u8Url', 'previewUrl', 'contentUrl'):
                        v = str(obj.get(vk, '') or '')
                        if v and ('m3u8' in v or 'mp4' in v):
                            meta['m3u8_url'] = v
                            break

                    # Source URL
                    meta['source_url'] = str(obj.get('url', '') or obj.get('pageUrl', '') or '')

                    is_new = self.db.save_clip(meta)
                    if is_new:
                        clips_found += 1
                    else:
                        self.db.update_metadata(cid, meta)

                    # Download thumbnail
                    if meta['thumbnail_url']:
                        tp = os.path.join(thumb_dir, f"{cid}.jpg")
                        if not os.path.isfile(tp):
                            self._download_thumb_url(meta['thumbnail_url'], tp, cid)

                for v in obj.values():
                    _walk(v)

            _walk(data)
            if clips_found:
                self.log(f"  [catalog-api] Intercepted {clips_found} new clips from API: {url[:70]}", "M3U8")
                self.stats_signal.emit(self.db.stats())

        except json.JSONDecodeError:
            pass
        except Exception as e:
            err = str(e)
            if not any(x in err for x in ('decode', 'Connection', 'Target closed', 'disposed')):
                pass  # Silent — don't spam log for every non-JSON response

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
                # Clip ID not in any video URL — common for sites like Artlist
                # where HLS URLs don't encode the clip ID.
                unknown_urls = by_vid_id.get('__unknown__', [])
                if total_ids == 1 and len(unknown_urls) <= 3:
                    # Only __unknown__ group with very few URLs → likely our clip
                    self.log(
                        f"  [scan] {len(unknown_urls)} untagged URL(s) — "
                        f"assuming clip id:{current_clip_id}",
                        "DEBUG")
                    best = self._pick_best_quality(unknown_urls)
                    await self._record_video_url(best, source_url, clip_meta)
                else:
                    # Many URLs without clip ID → related video previews.
                    # Response interceptor already captured the real video URL,
                    # so skip scan recording to avoid polluting DB.
                    self.log(
                        f"  [scan] Skipping {len(found_urls)} URLs across "
                        f"{total_ids} groups — no clip ID match, response "
                        f"interceptor has the primary video.",
                        "DEBUG")
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
            unverifiable = 0
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
                        if not vid_m:
                            unverifiable += 1
                            continue  # Can't verify ownership — skip
                    await self._record_video_url(u, source_url, clip_meta)
                    recorded += 1
            if recorded or skipped or unverifiable:
                self.log(
                    f"  [js-intercept] {recorded} recorded, {skipped} skipped "
                    f"(other clips), {unverifiable} skipped (unverifiable)",
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
# DIRECT HTTP SCRAPER  — bypasses browser for metadata collection
# ─────────────────────────────────────────────────────────────────────────────

class DirectScrapeWorker(QThread):
    """
    Lightweight HTTP-only metadata scraper. No browser rendering.
    Modes:
      - api_discover:  Launch browser once, capture all XHR/fetch traffic,
                       report API endpoints and shapes.
      - direct_http:   Fetch metadata via HTTP only: sitemaps, __NEXT_DATA__,
                       _next/data endpoints, and clip ID probing.
    """
    log_signal    = pyqtSignal(str, str)
    stats_signal  = pyqtSignal(dict)
    clip_signal   = pyqtSignal(dict)
    status_signal = pyqtSignal(str)
    finished      = pyqtSignal()

    # Shared HTTP headers to look like a real browser
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
    }

    def __init__(self, cfg, db, mode='direct_http'):
        super().__init__()
        self.cfg   = cfg
        self.db    = db
        self.mode  = mode
        self._stop = threading.Event()
        self._db_lock = threading.Lock()  # Thread-safe DB access for concurrent mode

    def stop(self): self._stop.set()

    def log(self, msg, level='INFO'):
        self.log_signal.emit(f"[{datetime.now().strftime('%H:%M:%S')}] [{level:5s}] {msg}", level)

    def run(self):
        self.status_signal.emit("running")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            if self.mode == 'api_discover':
                loop.run_until_complete(self._api_discover())
            else:
                loop.run_until_complete(self._direct_scrape())
        except Exception as e:
            import traceback as _tb
            self.log(f"DirectScrape crashed: {e}\n{_tb.format_exc()[:500]}", "ERROR")
        finally:
            try:
                loop.close()
            except Exception:
                pass
        self.status_signal.emit("stopped")
        self.finished.emit()

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _http_get(self, url, headers=None, timeout=15, max_size=10_000_000):
        """Simple HTTP GET that returns (status_code, body_bytes) or (0, b'') on error."""
        import urllib.request, urllib.error, gzip, io
        hdrs = dict(self.HEADERS)
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, headers=hdrs)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            data = resp.read(max_size)
            # Handle gzip
            if resp.headers.get('Content-Encoding') == 'gzip':
                data = gzip.decompress(data)
            return resp.status, data
        except urllib.error.HTTPError as e:
            return e.code, b''
        except Exception:
            return 0, b''

    def _http_get_json(self, url, headers=None, timeout=15):
        """HTTP GET returning parsed JSON or None."""
        hdrs = dict(self.HEADERS)
        hdrs['Accept'] = 'application/json, text/plain, */*'
        if headers:
            hdrs.update(headers)
        code, data = self._http_get(url, hdrs, timeout)
        if code == 200 and data:
            try:
                return json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
        return None

    def _save_clip_meta(self, meta):
        """Save/update clip in DB (thread-safe). Returns True if new."""
        clip_id = str(meta.get('clip_id', '') or '').strip()
        if not clip_id:
            return False
        with self._db_lock:
            is_new = self.db.save_clip(meta)
            if not is_new:
                self.db.update_metadata(clip_id, meta)
        return is_new

    def _download_thumb(self, url, clip_id, thumb_dir):
        """Download thumbnail to disk (thread-safe for DB update)."""
        if not url or not clip_id:
            return
        out = os.path.join(thumb_dir, f"{clip_id}.jpg")
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return
        try:
            import urllib.request
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'Mozilla/5.0')
            resp = urllib.request.urlopen(req, timeout=10)
            data = resp.read(2_000_000)
            resp.close()
            if len(data) > 500:
                with open(out, 'wb') as f:
                    f.write(data)
                with self._db_lock:
                    self.db.update_thumb_path(clip_id, out)
        except Exception:
            pass

    # ── API Discovery (browser-based, one-time) ──────────────────────────────

    async def _api_discover(self):
        """Launch browser, browse /stock-footage, capture all XHR/fetch traffic."""
        from playwright.async_api import async_playwright

        self.log("=== API DISCOVERY MODE ===", "OK")
        self.log("Launching browser to capture Artlist's internal API endpoints...", "INFO")

        discovered = {}  # url_pattern -> {method, content_type, sample_keys, count}
        clip_data_endpoints = []  # endpoints that returned clip-like data

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=self.HEADERS['User-Agent'],
                viewport={'width': 1440, 'height': 900},
            )
            page = await context.new_page()

            async def _on_resp(response):
                try:
                    url = response.url
                    rt = response.request.resource_type
                    if rt not in ('xhr', 'fetch', 'document'):
                        return
                    ct = response.headers.get('content-type', '')
                    method = response.request.method

                    # Normalize URL: strip query params for pattern matching
                    parsed = urlparse(url)
                    path = parsed.path
                    domain = parsed.netloc
                    pattern_key = f"{method} {domain}{path}"

                    if pattern_key not in discovered:
                        discovered[pattern_key] = {
                            'method': method,
                            'content_type': ct[:60],
                            'count': 0,
                            'sample_url': url[:200],
                            'sample_query': parsed.query[:200] if parsed.query else '',
                            'has_clip_data': False,
                            'response_keys': [],
                            'clip_count': 0,
                            'request_body': '',
                            'request_headers': {},
                        }
                    discovered[pattern_key]['count'] += 1

                    # Try to parse JSON responses for clip data
                    if 'json' in ct:
                        try:
                            body = await response.text()
                            if len(body) > 20 and len(body) < 5_000_000:
                                data = json.loads(body)
                                if isinstance(data, dict):
                                    discovered[pattern_key]['response_keys'] = list(data.keys())[:20]
                                clips_found = self._walk_for_clips(data)
                                if clips_found:
                                    discovered[pattern_key]['has_clip_data'] = True
                                    discovered[pattern_key]['clip_count'] += len(clips_found)
                                    # Capture request body for GraphQL endpoints
                                    req_body = response.request.post_data or ''
                                    req_hdrs = dict(response.request.headers)
                                    discovered[pattern_key]['request_body'] = req_body[:2000]
                                    discovered[pattern_key]['request_headers'] = {
                                        k: v for k, v in req_hdrs.items()
                                        if k.lower() not in ('host', 'content-length', 'connection',
                                                              'accept-encoding')
                                    }
                                    clip_data_endpoints.append({
                                        'url': url,
                                        'method': method,
                                        'clip_count': len(clips_found),
                                        'sample_clip': clips_found[0] if clips_found else {},
                                        'request_body': req_body[:2000],
                                    })
                        except Exception:
                            pass
                except Exception:
                    pass

            page.on('response', _on_resp)

            # ── Phase 1: Load main catalog page ──────────────────────────
            self.log("Phase 1: Loading /stock-footage ...", "INFO")
            await page.goto('https://artlist.io/stock-footage', wait_until='networkidle', timeout=30000)
            await asyncio.sleep(3)

            # ── Phase 2: Scroll to trigger lazy loading ──────────────────
            self.log("Phase 2: Scrolling to trigger API calls...", "INFO")
            for i in range(8):
                if self._stop.is_set():
                    break
                await page.evaluate("window.scrollBy(0, 800)")
                await asyncio.sleep(1.5)

            # ── Phase 3: Try a search query ──────────────────────────────
            self.log("Phase 3: Searching 'nature' to trigger search API...", "INFO")
            try:
                search_input = await page.query_selector('input[type="search"], input[type="text"], input[placeholder*="earch"]')
                if search_input:
                    await search_input.fill('nature')
                    await asyncio.sleep(0.5)
                    await search_input.press('Enter')
                    await asyncio.sleep(4)
                    # Scroll search results
                    for _ in range(4):
                        await page.evaluate("window.scrollBy(0, 600)")
                        await asyncio.sleep(1.5)
            except Exception as e:
                self.log(f"Search attempt: {e}", "DEBUG")

            # ── Phase 4: Try navigating to a clip page ───────────────────
            self.log("Phase 4: Loading a sample clip page...", "INFO")
            try:
                await page.goto(
                    'https://artlist.io/stock-footage/clip/buildings-traffic-new-york-usa/6451306',
                    wait_until='networkidle', timeout=20000)
                await asyncio.sleep(3)
            except Exception as e:
                self.log(f"Clip page: {e}", "DEBUG")

            # ── Phase 5: Extract __NEXT_DATA__ ───────────────────────────
            self.log("Phase 5: Extracting __NEXT_DATA__ buildId...", "INFO")
            try:
                next_data = await page.evaluate("""
                    (() => {
                        const nd = document.getElementById('__NEXT_DATA__');
                        if (nd) {
                            try {
                                const d = JSON.parse(nd.textContent);
                                return {
                                    buildId: d.buildId || null,
                                    top_keys: Object.keys(d),
                                    props_keys: d.props ? Object.keys(d.props) : [],
                                    page_props_keys: d.props?.pageProps ? Object.keys(d.props.pageProps) : [],
                                    page: d.page || null,
                                    has_clips: !!(JSON.stringify(d).match(/clipId|clip_id/i)),
                                    total_size: JSON.stringify(d).length,
                                };
                            } catch(e) { return {error: e.message}; }
                        }
                        return null;
                    })()
                """)
                if next_data:
                    self.log(f"  __NEXT_DATA__ found!", "OK")
                    self.log(f"    buildId: {next_data.get('buildId', 'N/A')}", "M3U8")
                    self.log(f"    page: {next_data.get('page', 'N/A')}", "INFO")
                    self.log(f"    top_keys: {next_data.get('top_keys', [])}", "INFO")
                    self.log(f"    props_keys: {next_data.get('props_keys', [])}", "INFO")
                    self.log(f"    pageProps keys: {next_data.get('page_props_keys', [])}", "INFO")
                    self.log(f"    has clip data: {next_data.get('has_clips', False)}", "INFO")
                    self.log(f"    total JSON size: {next_data.get('total_size', 0):,} bytes", "INFO")

                    # Save buildId for direct HTTP mode
                    build_id = next_data.get('buildId')
                    if build_id:
                        cfg = load_config()
                        cfg['artlist_build_id'] = build_id
                        save_config(cfg)
                        self.log(f"  Saved buildId to config: {build_id}", "OK")
                else:
                    self.log("  __NEXT_DATA__ not found on page", "WARN")
            except Exception as e:
                self.log(f"  __NEXT_DATA__ extraction error: {e}", "WARN")

            await browser.close()

        # ── Report results ────────────────────────────────────────────────
        self.log("", "INFO")
        self.log("=" * 70, "OK")
        self.log("  API DISCOVERY RESULTS", "OK")
        self.log("=" * 70, "OK")

        # Sort by count
        sorted_eps = sorted(discovered.items(), key=lambda x: x[1]['count'], reverse=True)

        # Show API-like endpoints (not static assets)
        self.log("", "INFO")
        self.log("--- XHR/Fetch Endpoints (sorted by frequency) ---", "INFO")
        for pattern, info in sorted_eps:
            if any(x in pattern for x in ['.js', '.css', '.png', '.jpg', '.svg', '.woff', '.ico', 'favicon']):
                continue
            clip_marker = " ** HAS CLIP DATA **" if info['has_clip_data'] else ""
            self.log(
                f"  [{info['count']:3d}x] {pattern[:80]}  "
                f"ct:{info['content_type'][:30]}{clip_marker}",
                "M3U8" if info['has_clip_data'] else "INFO")
            if info['sample_query']:
                self.log(f"         query: {info['sample_query'][:120]}", "DEBUG")
            if info['response_keys']:
                self.log(f"         keys: {info['response_keys']}", "DEBUG")

        self.log("", "INFO")
        if clip_data_endpoints:
            self.log(f"--- CLIP DATA ENDPOINTS ({len(clip_data_endpoints)} found) ---", "OK")
            for ep in clip_data_endpoints:
                self.log(f"  {ep['method']} {ep['url'][:120]}", "M3U8")
                self.log(f"    clips: {ep['clip_count']}", "INFO")
                if ep.get('sample_clip'):
                    sc = ep['sample_clip']
                    self.log(f"    sample: id={sc.get('clip_id','')} title={sc.get('title','')[:50]}", "INFO")
                if ep.get('request_body'):
                    try:
                        gql = json.loads(ep['request_body'])
                        op = gql.get('operationName', '')
                        vkeys = list(gql.get('variables', {}).keys())
                        self.log(f"    GraphQL op: {op}, variables: {vkeys}", "M3U8")
                        q = gql.get('query', '')
                        self.log(f"    query preview: {q[:120]}...", "DEBUG")
                    except Exception:
                        self.log(f"    request body: {ep['request_body'][:120]}...", "DEBUG")

            # Save the best GraphQL template
            best_ep = max(clip_data_endpoints, key=lambda x: x['clip_count'])
            if best_ep.get('request_body'):
                try:
                    gql = json.loads(best_ep['request_body'])
                    # Also find the matching discovered entry for headers
                    best_key = f"POST {urlparse(best_ep['url']).netloc}{urlparse(best_ep['url']).path}"
                    hdrs = discovered.get(best_key, {}).get('request_headers', {})
                    cfg = load_config()
                    cfg['artlist_graphql'] = {
                        'url': best_ep['url'],
                        'query': gql.get('query', ''),
                        'variables': gql.get('variables', {}),
                        'operation': gql.get('operationName', ''),
                        'headers': hdrs,
                    }
                    save_config(cfg)
                    self.log("", "INFO")
                    self.log("Saved GraphQL template to config for Direct HTTP mode.", "OK")
                except Exception:
                    pass
        else:
            self.log("  No endpoints returned clip data during browsing.", "WARN")

        self.log("", "INFO")
        self.log("Discovery complete. Now run 'Direct HTTP' mode to paginate the catalog.", "OK")

    def _walk_for_clips(self, obj, depth=0, _debug_first=False):
        """Walk a JSON structure looking for clip-like objects. Returns list of dicts."""
        if depth > 12:
            return []
        clips = []
        if isinstance(obj, list):
            for item in obj:
                clips.extend(self._walk_for_clips(item, depth + 1, _debug_first))
        elif isinstance(obj, dict):
            # Check if this looks like a clip object — accept numeric id 4+ digits
            cid = str(obj.get('id', '') or obj.get('clipId', '') or obj.get('clip_id', '') or
                       obj.get('assetId', '') or obj.get('asset_id', '') or '')
            if cid and re.match(r'^\d{4,}$', cid):
                # Log raw keys of first clip found (diagnostic for field mapping)
                if not hasattr(self, '_logged_sample_clip'):
                    self._logged_sample_clip = True
                    self.log(f"  [DIAG] Clip object keys: {sorted(obj.keys())[:30]}", "DEBUG")
                    for dk in list(obj.keys())[:20]:
                        dv = obj[dk]
                        if isinstance(dv, (str, int, float, bool)) and dv:
                            self.log(f"  [DIAG]   {dk} = {str(dv)[:100]}", "DEBUG")
                        elif isinstance(dv, dict):
                            self.log(f"  [DIAG]   {dk} = dict({list(dv.keys())[:8]})", "DEBUG")
                        elif isinstance(dv, list) and dv:
                            self.log(f"  [DIAG]   {dk} = list[{len(dv)}]", "DEBUG")
                # This has a clip-like ID — extract everything we can
                # ── Title ──
                title = str(
                    obj.get('clipName', '') or obj.get('title', '') or obj.get('name', '') or
                    obj.get('displayName', '') or
                    (obj.get('clipNameForUrl', '').replace('-', ' ').title() if obj.get('clipNameForUrl') else '') or
                    (obj.get('slug', '').replace('-', ' ').title() if obj.get('slug') else '') or
                    obj.get('description', '') or '')
                # ── Slug → URL ──
                slug = str(obj.get('clipNameForUrl', '') or obj.get('slug', '') or obj.get('urlSlug', '') or '')
                # ── Creator/artist ──
                creator = str(
                    obj.get('filmMakerDisplayName', '') or obj.get('filmmakerDisplayName', '') or
                    obj.get('artistName', '') or obj.get('artist_name', '') or
                    obj.get('creatorName', '') or obj.get('creator_name', '') or
                    obj.get('displayArtistName', '') or obj.get('authorName', '') or '')
                if not creator:
                    for ck in ('artist', 'creator', 'filmmaker', 'filmMaker', 'author', 'owner'):
                        av = obj.get(ck)
                        if isinstance(av, dict):
                            creator = str(av.get('name', '') or av.get('displayName', '') or
                                          av.get('fullName', '') or av.get('slug', '') or '')
                            break
                        elif isinstance(av, str) and av:
                            creator = av; break
                # ── Thumbnail ──
                thumb = ''
                for tk in ('thumbnailUrl', 'thumbnail_url', 'thumbnail', 'imageUrl',
                           'image_url', 'posterUrl', 'poster_url', 'coverUrl', 'cover_url',
                           'previewImageUrl', 'preview_image_url', 'image', 'poster', 'cover'):
                    tv = obj.get(tk)
                    if isinstance(tv, str) and tv.startswith('http'):
                        thumb = tv; break
                    elif isinstance(tv, dict) and tv.get('url'):
                        thumb = str(tv['url']); break
                # ── Duration (Artlist returns ms, convert to seconds) ──
                dur_raw = obj.get('duration', '') or obj.get('length', '') or obj.get('durationInSeconds', '') or ''
                if dur_raw:
                    try:
                        dur_val = int(dur_raw)
                        # Artlist returns ms (values >1000), convert to seconds
                        duration = str(round(dur_val / 1000, 1)) if dur_val > 1000 else str(dur_val)
                    except (ValueError, TypeError):
                        duration = str(dur_raw)
                else:
                    duration = ''
                # ── Resolution (Artlist uses width/height, not a 'resolution' field) ──
                w = obj.get('width', '')
                h = obj.get('height', '')
                resolution = str(obj.get('resolution', '') or obj.get('maxResolution', '') or obj.get('quality', '') or '')
                if not resolution and w and h:
                    resolution = f"{w}x{h}"
                # ── Source URL ──
                source_url = str(obj.get('url', '') or obj.get('pageUrl', '') or
                                  obj.get('shareUrl', '') or obj.get('link', '') or '')
                if not source_url and slug:
                    source_url = f'https://artlist.io/stock-footage/clip/{slug}/{cid}'
                # ── M3U8 / Video URL (clipPath is the HLS manifest!) ──
                m3u8 = ''
                for vk in ('clipPath', 'clip_path', 'videoUrl', 'video_url', 'hlsUrl', 'hls_url',
                           'previewUrl', 'preview_url', 'contentUrl', 'content_url',
                           'streamUrl', 'stream_url', 'previewVideoUrl', 'mp4Url'):
                    vv = obj.get(vk)
                    if isinstance(vv, str) and vv.startswith('http'):
                        m3u8 = vv; break
                # ── Tags (build from boolean flags if no tags array) ──
                tags_raw = obj.get('tags', obj.get('keywords', obj.get('labels', [])))
                if isinstance(tags_raw, str):
                    tags = tags_raw
                elif isinstance(tags_raw, list) and tags_raw:
                    tags = ', '.join(
                        str(t.get('name', '') if isinstance(t, dict) else t)
                        for t in tags_raw[:25])
                else:
                    # Build tags from Artlist boolean flags
                    flag_tags = []
                    if obj.get('isMadeWithAi'): flag_tags.append('AI-generated')
                    if obj.get('isOriginal'): flag_tags.append('Original')
                    if obj.get('isVfx'): flag_tags.append('VFX')
                    if obj.get('isNew'): flag_tags.append('New')
                    orient = obj.get('orientation', '')
                    if orient: flag_tags.append(str(orient).capitalize())
                    tags = ', '.join(flag_tags)
                # ── Collection/story ──
                collection = str(
                    obj.get('storyName', '') or obj.get('collectionName', '') or
                    obj.get('collection_name', '') or obj.get('story_name', '') or
                    obj.get('folderName', '') or obj.get('folder_name', '') or '')
                if not collection:
                    for stk in ('collection', 'story', 'folder'):
                        sv = obj.get(stk)
                        if isinstance(sv, dict):
                            collection = str(sv.get('name', '') or sv.get('title', '') or '')
                            break
                        elif isinstance(sv, str) and sv:
                            collection = sv; break
                # Only require the numeric ID — don't require title/thumb
                # (GraphQL responses may have minimal fields on first page)
                # ── Formats (Artlist uses availableFormats as a list) ──
                fmt_raw = obj.get('availableFormats', obj.get('formats', obj.get('available_formats', '')))
                if isinstance(fmt_raw, list):
                    formats = ', '.join(str(f) for f in fmt_raw)
                else:
                    formats = str(fmt_raw) if fmt_raw else ''
                clips.append({
                    'clip_id': cid,
                    'title': title,
                    'creator': creator,
                    'thumbnail_url': thumb,
                    'duration': duration,
                    'resolution': resolution,
                    'tags': tags,
                    'collection': collection,
                    'frame_rate': str(obj.get('fps', '') or obj.get('frameRate', '') or
                                      obj.get('frame_rate', '') or ''),
                    'camera': str(obj.get('camera', '') or obj.get('cameraModel', '') or
                                   obj.get('camera_model', '') or ''),
                    'formats': formats,
                    'source_url': source_url,
                    'm3u8_url': m3u8,
                })
                return clips  # Don't recurse into children of a clip object
            for v in obj.values():
                clips.extend(self._walk_for_clips(v, depth + 1, _debug_first))
        return clips

    # ── Direct HTTP Scrape (no browser) ───────────────────────────────────────

    async def _direct_scrape(self):
        """Main direct scrape pipeline: browser bootstrap → GraphQL pagination."""
        self.log("=== DIRECT HTTP SCRAPE MODE ===", "OK")

        thumb_dir = os.path.join(
            os.environ.get('APPDATA', os.path.expanduser('~')),
            'ArtlistScraper', 'thumbnails')
        os.makedirs(thumb_dir, exist_ok=True)

        total_new = 0
        total_updated = 0
        graphql_templates = []

        # Check for saved GraphQL template from prior API Discovery
        saved_gql = self.cfg.get('artlist_graphql')
        if not saved_gql:
            saved_gql = load_config().get('artlist_graphql')

        if saved_gql and saved_gql.get('query'):
            self.log("Found saved GraphQL template from prior API Discovery.", "OK")
            graphql_templates = [saved_gql]
        elif not self._stop.is_set():
            # ── Phase 0: Browser bootstrap to capture GraphQL templates (~30s) ──
            self.log("", "INFO")
            self.log("Phase 0: Browser bootstrap — capturing GraphQL queries (~30s)...", "INFO")
            try:
                _, graphql_templates, bootstrap_clips = await self._browser_bootstrap()
                if bootstrap_clips:
                    new, upd = self._ingest_clips(bootstrap_clips, thumb_dir, "bootstrap")
                    total_new += new
                    total_updated += upd
            except Exception as e:
                self.log(f"  Bootstrap error: {e}", "WARN")

        # ── Phase 1: GraphQL pagination (the main engine) ─────────────────
        if not self._stop.is_set() and graphql_templates:
            self.log("", "INFO")
            self.log("Phase 1: GraphQL pagination — scraping full catalog...", "OK")
            self._graphql_paginate(graphql_templates, thumb_dir)
            # Clips and thumbnails saved directly to DB inside paginator
        elif not graphql_templates:
            self.log("", "INFO")
            self.log("No GraphQL templates captured. Possible causes:", "WARN")
            self.log("  - Chromium not installed (install browser first)", "WARN")
            self.log("  - Cloudflare blocked the headless browser", "WARN")
            self.log("  - Site structure changed", "WARN")

        # ── Phase 2: Sitemap check (supplemental) ─────────────────────────
        if not self._stop.is_set():
            self.log("", "INFO")
            self.log("Phase 2: Checking sitemaps for additional URLs...", "INFO")
            sitemap_clips = self._check_sitemaps()
            if sitemap_clips:
                new, upd = self._ingest_clips(sitemap_clips, thumb_dir, "sitemap")
                total_new += new
                total_updated += upd

        # ── Summary ───────────────────────────────────────────────────────
        stats = self.db.stats()
        self.log("", "INFO")
        self.log("=" * 60, "OK")
        self.log(f"  DIRECT HTTP SCRAPE COMPLETE", "OK")
        self.log(f"  Total clips in database: {stats.get('clips', 0):,}", "OK")
        self.log("=" * 60, "OK")
        self.stats_signal.emit(stats)

    # ── Browser bootstrap ─────────────────────────────────────────────────────

    async def _browser_bootstrap(self):
        """Quick browser session to capture GraphQL queries + headers for replay."""
        api_urls = []
        clips = []
        graphql_templates = []  # list of {query, variables, headers, clip_count}

        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            # Check if chromium is actually installed
            exe = pw.chromium.executable_path
            if not os.path.isfile(exe):
                self.log("  Chromium not installed — skipping bootstrap.", "WARN")
                self.log("  Install browser, then re-run; or use 'API Discovery' mode first.", "WARN")
                return '', api_urls, clips
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=self.HEADERS['User-Agent'],
                viewport={'width': 1440, 'height': 900},
            )
            page = await context.new_page()

            async def _on_resp(response):
                try:
                    url = response.url
                    rt = response.request.resource_type
                    if rt not in ('xhr', 'fetch'):
                        return
                    ct = response.headers.get('content-type', '')
                    if 'json' not in ct:
                        return

                    # Only care about the GraphQL endpoint
                    if 'graphql' not in url.lower():
                        return

                    body = await response.text()
                    if len(body) < 50 or len(body) > 5_000_000:
                        return
                    resp_data = json.loads(body)

                    # Check if this response has clip data
                    found_clips = self._walk_for_clips(resp_data)
                    if not found_clips:
                        return

                    clips.extend(found_clips)

                    # Capture the REQUEST body (GraphQL query + variables)
                    req = response.request
                    req_body = req.post_data
                    req_headers = {k: v for k, v in req.headers.items()
                                   if k.lower() not in ('host', 'content-length', 'connection',
                                                         'accept-encoding', 'origin', 'referer')}

                    if req_body:
                        try:
                            gql = json.loads(req_body)
                            graphql_templates.append({
                                'url': url,
                                'query': gql.get('query', ''),
                                'variables': gql.get('variables', {}),
                                'operation': gql.get('operationName', ''),
                                'headers': req_headers,
                                'clip_count': len(found_clips),
                            })
                        except json.JSONDecodeError:
                            pass

                except Exception:
                    pass

            page.on('response', _on_resp)

            # Navigate to catalog and scroll to trigger GraphQL calls
            self.log("  Loading /stock-footage ...", "INFO")
            try:
                await page.goto('https://artlist.io/stock-footage',
                                wait_until='networkidle', timeout=30000)
            except Exception:
                try:
                    await page.goto('https://artlist.io/stock-footage', timeout=30000)
                except Exception as e:
                    self.log(f"  Page load error: {e}", "WARN")
                    await browser.close()
                    return '', api_urls, clips

            await asyncio.sleep(3)

            # Scroll to trigger lazy-loaded GraphQL calls
            for i in range(6):
                if self._stop.is_set():
                    break
                await page.evaluate("window.scrollBy(0, 1000)")
                await asyncio.sleep(1.5)

            await browser.close()

        # Deduplicate clips
        seen_ids = set()
        unique_clips = []
        for c in clips:
            cid = c.get('clip_id', '')
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                unique_clips.append(c)

        self.log(f"  Captured {len(graphql_templates)} GraphQL templates, "
                 f"{len(unique_clips)} unique clips", "OK")

        # Log GraphQL details
        for i, tpl in enumerate(graphql_templates):
            op = tpl.get('operation', 'unknown')
            vc = tpl.get('clip_count', 0)
            vkeys = list(tpl.get('variables', {}).keys())
            self.log(f"  Template {i}: operation={op}, clips={vc}, vars={vkeys}", "INFO")

        # Save best GraphQL template to config for pure HTTP replay
        if graphql_templates:
            # Pick the template that returned the most clips
            best = max(graphql_templates, key=lambda x: x['clip_count'])
            cfg = load_config()
            cfg['artlist_graphql'] = {
                'url': best['url'],
                'query': best['query'],
                'variables': best['variables'],
                'operation': best.get('operation', ''),
                'headers': best['headers'],
            }
            save_config(cfg)
            self.log(f"  Saved GraphQL template: operation={best.get('operation','')}, "
                     f"url={best['url'][:60]}", "M3U8")

        return '', graphql_templates, unique_clips

    # ── Phase 2: Scrape discovered API endpoints ──────────────────────────────

    # ── Stock footage keyword universe ──────────────────────────────────────
    SEARCH_TERMS = [
        # Nature & landscapes
        'nature', 'landscape', 'mountain', 'forest', 'ocean', 'river', 'lake',
        'waterfall', 'desert', 'canyon', 'valley', 'field', 'meadow', 'prairie',
        'jungle', 'rainforest', 'swamp', 'marsh', 'glacier', 'volcano', 'island',
        'beach', 'coast', 'cliff', 'cave', 'coral reef', 'tundra', 'savanna',
        # Sky & weather
        'sky', 'clouds', 'sunset', 'sunrise', 'storm', 'lightning', 'rain',
        'snow', 'fog', 'mist', 'rainbow', 'aurora', 'stars', 'moon', 'sun',
        'wind', 'tornado', 'hurricane', 'blizzard', 'hail',
        # Water
        'water', 'wave', 'splash', 'underwater', 'diving', 'swimming', 'surfing',
        'pond', 'stream', 'rapids', 'dam', 'fountain', 'ice', 'frost', 'dew',
        # Animals
        'animal', 'wildlife', 'bird', 'fish', 'horse', 'dog', 'cat', 'lion',
        'elephant', 'bear', 'wolf', 'deer', 'eagle', 'butterfly', 'insect',
        'whale', 'dolphin', 'shark', 'turtle', 'snake', 'monkey', 'gorilla',
        'penguin', 'flamingo', 'owl', 'hawk', 'parrot', 'jellyfish', 'octopus',
        'coral', 'bee', 'ant', 'spider', 'frog', 'lizard', 'crocodile',
        # Plants & flowers
        'flower', 'tree', 'plant', 'garden', 'grass', 'leaf', 'bloom', 'rose',
        'tulip', 'sunflower', 'cherry blossom', 'palm tree', 'cactus', 'moss',
        'mushroom', 'vine', 'bamboo', 'fern', 'pine', 'oak', 'maple',
        # Urban & architecture
        'city', 'street', 'building', 'skyscraper', 'bridge', 'road', 'highway',
        'traffic', 'downtown', 'skyline', 'architecture', 'window', 'door',
        'stairs', 'elevator', 'parking', 'tunnel', 'alley', 'rooftop', 'balcony',
        'suburb', 'neighborhood', 'apartment', 'house', 'mansion', 'castle',
        'church', 'mosque', 'temple', 'monument', 'statue', 'fountain', 'plaza',
        'market', 'mall', 'shop', 'restaurant', 'cafe', 'bar', 'hotel',
        # People & lifestyle
        'people', 'woman', 'man', 'child', 'baby', 'family', 'couple', 'friends',
        'crowd', 'portrait', 'face', 'hands', 'eyes', 'smile', 'walking',
        'running', 'dancing', 'laughing', 'crying', 'talking', 'eating',
        'drinking', 'sleeping', 'working', 'reading', 'writing', 'cooking',
        'shopping', 'traveling', 'hiking', 'camping', 'fishing', 'climbing',
        'yoga', 'meditation', 'exercise', 'gym', 'sports', 'football',
        'basketball', 'soccer', 'tennis', 'golf', 'cycling', 'skateboarding',
        'surfing', 'skiing', 'snowboarding', 'marathon', 'boxing', 'wrestling',
        # Aerial & drone
        'drone', 'aerial', 'bird eye', 'flyover', 'overhead', 'top down',
        'helicopter', 'panoramic', 'orbit', 'reveal',
        # Technology & science
        'technology', 'computer', 'laptop', 'phone', 'screen', 'code', 'data',
        'network', 'server', 'robot', 'AI', 'machine', 'circuit', 'chip',
        'laboratory', 'microscope', 'telescope', 'satellite', 'radar', 'antenna',
        'solar panel', 'wind turbine', 'nuclear', 'electric', 'battery', 'cable',
        'fiber optic', 'hologram', 'VR', 'drone technology',
        # Business & office
        'business', 'office', 'meeting', 'conference', 'presentation', 'handshake',
        'teamwork', 'brainstorming', 'whiteboard', 'desk', 'chair', 'corporate',
        'startup', 'entrepreneur', 'leader', 'CEO', 'interview', 'hiring',
        # Industry & manufacturing
        'factory', 'manufacturing', 'industrial', 'warehouse', 'construction',
        'crane', 'welding', 'assembly', 'conveyor', 'machinery', 'tools',
        'steel', 'concrete', 'wood', 'mining', 'oil', 'gas', 'pipeline',
        'power plant', 'refinery', 'shipping', 'container', 'port', 'dock',
        # Transport
        'car', 'truck', 'bus', 'train', 'airplane', 'helicopter', 'boat',
        'ship', 'yacht', 'motorcycle', 'bicycle', 'subway', 'taxi', 'ambulance',
        'fire truck', 'police car', 'rocket', 'spaceship', 'airport', 'railway',
        'highway', 'intersection', 'parking lot', 'garage',
        # Food & drink
        'food', 'cooking', 'kitchen', 'chef', 'baking', 'grilling', 'barbecue',
        'pizza', 'pasta', 'sushi', 'salad', 'fruit', 'vegetable', 'meat',
        'bread', 'cake', 'chocolate', 'coffee', 'tea', 'wine', 'beer',
        'cocktail', 'juice', 'milk', 'cheese', 'ice cream', 'breakfast',
        'lunch', 'dinner', 'feast', 'picnic', 'market',
        # Art & music
        'art', 'painting', 'sculpture', 'museum', 'gallery', 'canvas', 'brush',
        'music', 'guitar', 'piano', 'drums', 'violin', 'concert', 'orchestra',
        'DJ', 'headphones', 'microphone', 'speaker', 'stage', 'performance',
        'theater', 'cinema', 'camera', 'photography', 'studio', 'film',
        # Abstract & visual
        'abstract', 'texture', 'pattern', 'geometric', 'fractal', 'particles',
        'smoke', 'fire', 'explosion', 'sparks', 'glitter', 'bokeh', 'lens flare',
        'light', 'shadow', 'silhouette', 'reflection', 'mirror', 'glass',
        'crystal', 'bubble', 'liquid', 'ink', 'paint', 'color', 'neon',
        'glow', 'laser', 'holographic', 'prism', 'gradient', 'motion blur',
        # Time & motion
        'timelapse', 'slow motion', 'hyperlapse', 'long exposure', 'fast motion',
        'time lapse', 'stop motion', 'spinning', 'rotating', 'flowing',
        'falling', 'rising', 'floating', 'flying', 'sinking',
        # Seasons & holidays
        'spring', 'summer', 'autumn', 'fall', 'winter', 'christmas', 'halloween',
        'easter', 'valentine', 'new year', 'thanksgiving', 'fireworks', 'parade',
        'carnival', 'festival', 'celebration', 'wedding', 'birthday', 'party',
        # Health & medical
        'medical', 'hospital', 'doctor', 'nurse', 'surgery', 'medicine',
        'pharmacy', 'health', 'fitness', 'wellness', 'spa', 'massage',
        'dental', 'x-ray', 'MRI', 'DNA', 'cell', 'virus', 'bacteria',
        # Education & learning
        'school', 'university', 'classroom', 'student', 'teacher', 'book',
        'library', 'study', 'graduation', 'lecture', 'chalkboard', 'notebook',
        # Agriculture & farming
        'farm', 'agriculture', 'harvest', 'tractor', 'field', 'crop', 'wheat',
        'corn', 'rice', 'vineyard', 'orchard', 'barn', 'cattle', 'sheep',
        'chicken', 'pig', 'milk', 'eggs', 'organic', 'greenhouse',
        # Space & science
        'space', 'galaxy', 'nebula', 'planet', 'earth', 'mars', 'jupiter',
        'asteroid', 'comet', 'constellation', 'milky way', 'black hole',
        'astronaut', 'ISS', 'rocket launch', 'observatory',
        # Emotions & concepts
        'love', 'happiness', 'sadness', 'anger', 'fear', 'hope', 'freedom',
        'peace', 'war', 'protest', 'revolution', 'poverty', 'wealth',
        'success', 'failure', 'dream', 'memory', 'time', 'death', 'life',
        'birth', 'growth', 'decay', 'change', 'transformation',
        # Materials & surfaces
        'metal', 'rust', 'gold', 'silver', 'copper', 'chrome', 'marble',
        'granite', 'sand', 'gravel', 'dirt', 'mud', 'clay', 'fabric',
        'leather', 'paper', 'cardboard', 'plastic', 'rubber', 'wax',
        # Misc high-coverage
        'cinematic', 'vintage', 'retro', 'futuristic', 'minimalist', 'luxury',
        'grunge', 'urban decay', 'abandoned', 'ruins', 'wreck', 'junkyard',
        'graffiti', 'neon sign', 'billboard', 'flag', 'clock', 'compass',
        'map', 'globe', 'chain', 'rope', 'wire', 'fence', 'gate', 'lock',
        'key', 'candle', 'flame', 'lantern', 'lamp', 'chandelier', 'window light',
        'shadow play', 'silhouette', 'backlit', 'golden hour', 'blue hour',
        'overcast', 'cloudy', 'sunny', 'rainy', 'snowy', 'windy', 'calm',
        'peaceful', 'chaotic', 'crowded', 'empty', 'lonely', 'isolated',
        'remote', 'hidden', 'secret', 'mysterious', 'dark', 'bright',
        'colorful', 'monochrome', 'black and white', 'sepia', 'warm', 'cool',
        'soft', 'sharp', 'blurry', 'crisp', 'smooth', 'rough', 'wet', 'dry',
    ]

    def _graphql_paginate(self, graphql_templates, thumb_dir):
        """
        Concurrent multi-strategy GraphQL pagination.
        Uses ThreadPoolExecutor for parallel queries, defers thumbnail downloads.
        """
        import time
        import urllib.request
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not graphql_templates:
            return []

        tpl = graphql_templates[0] if isinstance(graphql_templates[0], dict) else graphql_templates[0]
        gql_url = tpl.get('url', 'https://search-api.artlist.io/v1/graphql')
        gql_query = tpl.get('query', '')
        gql_vars = dict(tpl.get('variables', {}))
        gql_op = tpl.get('operation', '')
        gql_headers = dict(tpl.get('headers', {}))

        if not gql_query:
            self.log("  No GraphQL query captured.", "WARN")
            return []

        self.log(f"  Endpoint: {gql_url}", "INFO")
        self.log(f"  Operation: {gql_op or '(unnamed)'}", "INFO")
        self.log(f"  Variables: {list(gql_vars.keys())}", "INFO")
        self.log(f"  Search terms: {len(self.SEARCH_TERMS)}", "INFO")

        # Build request headers
        req_headers = dict(self.HEADERS)
        req_headers['Content-Type'] = 'application/json'
        req_headers['Accept'] = 'application/json'
        if gql_headers:
            for k, v in gql_headers.items():
                if k.lower() not in ('host', 'content-length', 'connection',
                                      'accept-encoding', 'user-agent', 'accept',
                                      'content-type'):
                    req_headers[k] = v

        # ── Thread-safe shared state ──────────────────────────────────────
        import threading
        state_lock = threading.Lock()  # single lock for ALL shared state
        seen_ids = set()
        total_new = [0]
        total_api = [0]
        rate_backoff_until = [0.0]  # timestamp when rate limit expires

        max_pages = 40  # API caps at ~33
        workers = self.cfg.get('concurrent_workers', 6)

        def _gql_post(vars_dict):
            """Single GraphQL POST. Returns parsed clips list or empty."""
            if self._stop.is_set():
                return []

            # Global rate-limit backoff
            now = time.time()
            with state_lock:
                wait_until = rate_backoff_until[0]
            if now < wait_until:
                time.sleep(wait_until - now + random.uniform(0.5, 2))

            payload = json.dumps({
                'query': gql_query,
                'variables': vars_dict,
                'operationName': gql_op or None,
            }).encode('utf-8')

            try:
                req = urllib.request.Request(gql_url, data=payload,
                                             headers=req_headers, method='POST')
                resp = urllib.request.urlopen(req, timeout=15)
                body = resp.read(5_000_000)
                resp.close()
                if resp.headers.get('Content-Encoding') == 'gzip':
                    import gzip
                    body = gzip.decompress(body)
                data = json.loads(body)
                with state_lock:
                    total_api[0] += 1
                return self._walk_for_clips(data)
            except Exception as e:
                err = str(e)
                if '403' in err or '429' in err:
                    with state_lock:
                        rate_backoff_until[0] = time.time() + 30
                return []

        def _run_query(label, var_overrides):
            """Paginate one query. Save to DB immediately, no thumbnails."""
            new_count = 0
            api_empty = 0      # API returned 0 clips (catalog exhausted)
            all_dupes = 0      # API returned clips but all already seen

            for pg in range(1, max_pages + 1):
                if self._stop.is_set():
                    break

                v = json.loads(json.dumps(gql_vars))
                v.update(var_overrides)
                v['page'] = pg

                found = _gql_post(v)

                if not found:
                    # API returned zero clips — truly empty page
                    api_empty += 1
                    if api_empty >= 2:
                        break
                    continue

                # API returned clips — reset api_empty counter
                api_empty = 0

                # Deduplicate against global seen set
                with state_lock:
                    fresh = [c for c in found
                             if c.get('clip_id') and c['clip_id'] not in seen_ids]
                    for c in fresh:
                        seen_ids.add(c['clip_id'])

                if fresh:
                    new_count += len(fresh)
                    all_dupes = 0
                    # Save to DB immediately (thread-safe via _db_lock)
                    for c in fresh:
                        c.setdefault('source_site', 'Artlist')
                        self._save_clip_meta(c)
                else:
                    # All clips on this page were already seen
                    all_dupes += 1
                    if all_dupes >= 10:
                        # 10 consecutive pages of pure overlap — move on
                        break

                time.sleep(random.uniform(0.12, 0.30))

            # Update global counter
            with state_lock:
                total_new[0] += new_count

            return label, new_count

        # ══════════════════════════════════════════════════════════════════
        # Build job queue
        # ══════════════════════════════════════════════════════════════════
        jobs = []

        # Sort rotations
        default_sort = gql_vars.get('sortType', 0)
        for sv in [default_sort, 1, 2, 3, 4, 5]:
            lbl = 'default' if sv == default_sort else f'sort-{sv}'
            jobs.append((lbl, {'sortType': sv}))

        # AI content toggle
        if 'includeAIContent' in gql_vars and not gql_vars.get('includeAIContent'):
            for sv in [default_sort, 1, 2, 3]:
                jobs.append((f'ai-sort{sv}', {'includeAIContent': True, 'sortType': sv}))

        # queryType exploration (0-9)
        if 'queryType' in gql_vars:
            dqt = gql_vars.get('queryType', 0)
            for qt in range(10):
                if qt != dqt:
                    jobs.append((f'qtype-{qt}', {'queryType': qt}))

        # Search terms (deduplicated)
        used = set()
        for term in self.SEARCH_TERMS:
            if term in used:
                continue
            used.add(term)
            jobs.append((f's:{term}', {'searchTerms': [term]}))

        # Search + AI content (separate pass, only unique terms)
        if 'includeAIContent' in gql_vars and not gql_vars.get('includeAIContent'):
            used2 = set()
            for term in self.SEARCH_TERMS:
                if term in used2:
                    continue
                used2.add(term)
                jobs.append((f's:{term}+ai', {'searchTerms': [term], 'includeAIContent': True}))

        # Search + sort combinations for top terms (maximizes unique results)
        top_terms = [
            'nature', 'city', 'people', 'water', 'sky', 'forest', 'ocean',
            'mountain', 'sunset', 'night', 'animal', 'drone', 'aerial',
            'technology', 'business', 'abstract', 'timelapse', 'slow motion',
            'food', 'car', 'fire', 'space', 'underwater', 'rain', 'snow',
        ]
        for term in top_terms:
            for sv in [1, 2, 3]:
                if sv == default_sort:
                    continue
                jobs.append((f's:{term}/s{sv}', {'searchTerms': [term], 'sortType': sv}))

        self.log(f"  Jobs queued: {len(jobs)}", "OK")
        self.log(f"  Workers: {workers}", "INFO")
        self.log(f"  Thumbnails: deferred (metadata-only scrape)", "INFO")
        self.log("", "INFO")

        # ══════════════════════════════════════════════════════════════════
        # Execute
        # ══════════════════════════════════════════════════════════════════
        completed = 0
        t_start = time.time()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {}
            for lbl, ovr in jobs:
                if self._stop.is_set():
                    break
                future_map[executor.submit(_run_query, lbl, ovr)] = lbl

            for fut in as_completed(future_map):
                if self._stop.is_set():
                    executor.shutdown(wait=False)
                    break
                try:
                    label, count = fut.result(timeout=180)
                except Exception:
                    label, count = future_map.get(fut, '?'), 0

                completed += 1

                if count > 0:
                    with state_lock:
                        t = total_new[0]
                        a = total_api[0]
                    self.log(
                        f"  [{completed}/{len(jobs)}] {label}: +{count:,} "
                        f"(total: {t:,}, calls: {a})", "OK")

                # Progress summary
                if completed % 25 == 0:
                    with state_lock:
                        t = total_new[0]
                        a = total_api[0]
                    elapsed = time.time() - t_start
                    rate = t / elapsed * 60 if elapsed > 0 else 0
                    self.log(
                        f"  --- {completed}/{len(jobs)} jobs | "
                        f"{t:,} clips | {a} calls | "
                        f"{rate:,.0f} clips/min | "
                        f"{elapsed:.0f}s elapsed ---", "M3U8")
                    self.stats_signal.emit(self.db.stats())

        # ══════════════════════════════════════════════════════════════════
        # Thumbnail download pass (after all metadata scraped)
        # ══════════════════════════════════════════════════════════════════
        with state_lock:
            final_total = total_new[0]
            final_api = total_api[0]

        elapsed = time.time() - t_start
        self.log("", "INFO")
        self.log(f"  Metadata scrape complete: {final_total:,} clips, "
                 f"{final_api} API calls, {elapsed:.0f}s", "OK")
        self.stats_signal.emit(self.db.stats())

        # Download thumbnails in parallel (non-blocking, best-effort)
        self.log(f"  Downloading thumbnails for {final_total:,} clips...", "INFO")
        thumb_count = [0]

        def _dl_thumb(clip_id, url):
            if self._stop.is_set():
                return
            out = os.path.join(thumb_dir, f"{clip_id}.jpg")
            if os.path.isfile(out) and os.path.getsize(out) > 0:
                return
            try:
                req = urllib.request.Request(url)
                req.add_header('User-Agent', 'Mozilla/5.0')
                resp = urllib.request.urlopen(req, timeout=8)
                data = resp.read(2_000_000)
                resp.close()
                if len(data) > 500:
                    with open(out, 'wb') as f:
                        f.write(data)
                    with self._db_lock:
                        self.db.update_thumb_path(clip_id, out)
                    with state_lock:
                        thumb_count[0] += 1
            except Exception:
                pass

        # Get all clips needing thumbnails from DB
        try:
            cur = self.db.conn.execute(
                "SELECT clip_id, thumbnail_url FROM clips "
                "WHERE thumbnail_url != '' AND thumbnail_url IS NOT NULL "
                "AND (thumb_path IS NULL OR thumb_path = '')")
            thumb_jobs = cur.fetchall()
        except Exception:
            thumb_jobs = []

        if thumb_jobs:
            self.log(f"  {len(thumb_jobs):,} thumbnails to download...", "INFO")
            with ThreadPoolExecutor(max_workers=10) as tex:
                futs = []
                for cid, turl in thumb_jobs:
                    if self._stop.is_set():
                        break
                    futs.append(tex.submit(_dl_thumb, cid, turl))
                # Wait with progress
                done = 0
                for f in as_completed(futs):
                    done += 1
                    if done % 500 == 0:
                        with state_lock:
                            tc = thumb_count[0]
                        self.log(f"    Thumbnails: {tc:,} downloaded ({done:,}/{len(thumb_jobs):,} processed)", "INFO")
                    if self._stop.is_set():
                        tex.shutdown(wait=False)
                        break
            with state_lock:
                tc = thumb_count[0]
            self.log(f"  Thumbnails complete: {tc:,} downloaded", "OK")
        self.stats_signal.emit(self.db.stats())

        return []  # clips already saved to DB; no need to return list

    def _ingest_clips(self, clips, thumb_dir, source_label):
        """Save a batch of clip dicts to DB, download thumbs. Returns (new, updated)."""
        new_count = 0
        upd_count = 0
        for clip in clips:
            if self._stop.is_set():
                break
            clip.setdefault('source_site', 'Artlist')
            is_new = self._save_clip_meta(clip)
            if is_new:
                new_count += 1
            else:
                upd_count += 1
            # Download thumbnail
            thumb_url = clip.get('thumbnail_url', '')
            clip_id = clip.get('clip_id', '')
            if thumb_url and clip_id:
                self._download_thumb(thumb_url, clip_id, thumb_dir)

        self.log(f"  [{source_label}] Ingested {new_count} new + {upd_count} updated clips", "OK" if new_count else "INFO")
        self.stats_signal.emit(self.db.stats())
        return new_count, upd_count

    # ── Phase 1: Sitemaps ─────────────────────────────────────────────────────

    def _check_sitemaps(self):
        """Check robots.txt and sitemap.xml for clip URLs."""
        clips = []

        # Try robots.txt for sitemap references
        self.log("  Fetching robots.txt...", "DEBUG")
        code, body = self._http_get('https://artlist.io/robots.txt')
        sitemap_urls = []
        if code == 200 and body:
            text = body.decode('utf-8', errors='replace')
            self.log(f"  robots.txt: {len(text)} bytes", "INFO")
            for line in text.splitlines():
                if line.lower().startswith('sitemap:'):
                    sm_url = line.split(':', 1)[1].strip()
                    sitemap_urls.append(sm_url)
                    self.log(f"  Found sitemap: {sm_url}", "M3U8")

        # Try common sitemap URLs
        for url in ['https://artlist.io/sitemap.xml',
                     'https://artlist.io/sitemap-index.xml',
                     'https://artlist.io/sitemaps/sitemap.xml'] + sitemap_urls:
            if self._stop.is_set():
                break
            self.log(f"  Fetching {url}...", "DEBUG")
            code, body = self._http_get(url, timeout=20)
            if code == 200 and body:
                text = body.decode('utf-8', errors='replace')
                self.log(f"  Sitemap response: {len(text)} bytes", "INFO")

                # Extract clip URLs from sitemap XML
                clip_urls = re.findall(
                    r'<loc>(https?://artlist\.io/stock-footage/clip/[^<]+)</loc>', text)
                self.log(f"  Found {len(clip_urls)} clip URLs in sitemap", "OK" if clip_urls else "INFO")

                for curl in clip_urls:
                    if self._stop.is_set():
                        break
                    cid_m = re.search(r'/(\d{4,})/?$', curl)
                    if cid_m:
                        slug_m = re.search(r'/clip/([^/]+)/', curl)
                        title = slug_m.group(1).replace('-', ' ').title() if slug_m else ''
                        clips.append({
                            'clip_id': cid_m.group(1),
                            'source_url': curl,
                            'title': title,
                            'source_site': 'Artlist',
                        })

                # Check for sub-sitemaps (sitemap index)
                sub_sitemaps = re.findall(r'<loc>(https?://[^<]*sitemap[^<]*\.xml[^<]*)</loc>', text)
                for sub_url in sub_sitemaps[:20]:  # cap at 20
                    if self._stop.is_set():
                        break
                    if 'footage' in sub_url.lower() or 'clip' in sub_url.lower() or 'video' in sub_url.lower():
                        self.log(f"  Fetching sub-sitemap: {sub_url[:80]}", "INFO")
                        sc, sb = self._http_get(sub_url, timeout=30)
                        if sc == 200 and sb:
                            st = sb.decode('utf-8', errors='replace')
                            sub_clips = re.findall(
                                r'<loc>(https?://artlist\.io/stock-footage/clip/[^<]+)</loc>', st)
                            self.log(f"  Sub-sitemap: {len(sub_clips)} clip URLs", "OK" if sub_clips else "INFO")
                            for curl in sub_clips:
                                cid_m = re.search(r'/(\d{4,})/?$', curl)
                                if cid_m:
                                    slug_m = re.search(r'/clip/([^/]+)/', curl)
                                    title = slug_m.group(1).replace('-', ' ').title() if slug_m else ''
                                    clips.append({
                                        'clip_id': cid_m.group(1),
                                        'source_url': curl,
                                        'title': title,
                                        'source_site': 'Artlist',
                                    })

        if clips:
            self.log(f"  Sitemaps total: {len(clips)} clip URLs", "OK")
        else:
            self.log("  No clip URLs found in sitemaps (site may not expose them).", "WARN")
        return clips

    # ── Phase 2: __NEXT_DATA__ from catalog page ──────────────────────────────

    def _fetch_next_data_catalog(self):
        """Fetch the catalog page HTML and extract __NEXT_DATA__ JSON."""
        clips = []
        build_id = ''

        self.log("  Fetching https://artlist.io/stock-footage ...", "INFO")
        code, body = self._http_get('https://artlist.io/stock-footage', timeout=20)
        if code != 200:
            self.log(f"  HTTP {code} — catalog page not accessible without JS rendering", "WARN")
            return clips, build_id

        html = body.decode('utf-8', errors='replace')
        self.log(f"  Response: {len(html):,} bytes", "INFO")

        # Extract __NEXT_DATA__
        nd_match = re.search(
            r'<script\s+id="__NEXT_DATA__"\s+type="application/json"[^>]*>(.*?)</script>',
            html, re.DOTALL)
        if not nd_match:
            self.log("  __NEXT_DATA__ not found in HTML", "WARN")
            # Try fetching a specific clip page instead
            self.log("  Trying a clip page for __NEXT_DATA__...", "INFO")
            code2, body2 = self._http_get(
                'https://artlist.io/stock-footage/clip/buildings-traffic-new-york-usa/6451306', timeout=20)
            if code2 == 200:
                html = body2.decode('utf-8', errors='replace')
                nd_match = re.search(
                    r'<script\s+id="__NEXT_DATA__"\s+type="application/json"[^>]*>(.*?)</script>',
                    html, re.DOTALL)

        if nd_match:
            try:
                nd_json = json.loads(nd_match.group(1))
                build_id = nd_json.get('buildId', '')
                self.log(f"  buildId: {build_id}", "M3U8" if build_id else "WARN")
                self.log(f"  page: {nd_json.get('page', 'N/A')}", "INFO")
                self.log(f"  JSON size: {len(nd_match.group(1)):,} bytes", "INFO")

                if build_id:
                    cfg = load_config()
                    cfg['artlist_build_id'] = build_id
                    save_config(cfg)

                # Walk the entire __NEXT_DATA__ for clip objects
                found = self._walk_for_clips(nd_json)
                self.log(f"  Found {len(found)} clips in __NEXT_DATA__", "OK" if found else "INFO")
                clips.extend(found)

            except json.JSONDecodeError as e:
                self.log(f"  __NEXT_DATA__ JSON parse error: {e}", "WARN")
        else:
            self.log("  __NEXT_DATA__ not found on any page (may require JS rendering)", "WARN")

        return clips, build_id

    # ── Phase 3: _next/data API ───────────────────────────────────────────────

    def _fetch_next_data_api(self, build_id):
        """Try Next.js _next/data/{buildId}/ JSON endpoints for paginated data."""
        clips = []
        if not build_id:
            return clips

        # Try various _next/data paths that Next.js might expose
        paths_to_try = [
            '/stock-footage.json',
            '/stock-footage/index.json',
            '/stock-footage.json?page=1',
            '/stock-footage.json?offset=0&limit=50',
        ]

        for path in paths_to_try:
            if self._stop.is_set():
                break
            url = f'https://artlist.io/_next/data/{build_id}{path}'
            self.log(f"  Trying: {url[:80]}", "DEBUG")
            data = self._http_get_json(url)
            if data:
                self.log(f"  Got JSON response! Keys: {list(data.keys())[:10]}", "M3U8")
                found = self._walk_for_clips(data)
                if found:
                    self.log(f"  Found {len(found)} clips", "OK")
                    clips.extend(found)

                    # Try pagination
                    page_num = 2
                    while page_num <= 100 and not self._stop.is_set():
                        page_url = f'https://artlist.io/_next/data/{build_id}/stock-footage.json?page={page_num}'
                        pdata = self._http_get_json(page_url)
                        if not pdata:
                            break
                        pfound = self._walk_for_clips(pdata)
                        if not pfound:
                            break
                        clips.extend(pfound)
                        self.log(f"  Page {page_num}: +{len(pfound)} clips (total: {len(clips)})", "INFO")
                        page_num += 1
                    break  # Found a working path, don't try others
            else:
                self.log(f"  No response from {path}", "DEBUG")

        if not clips:
            self.log("  _next/data endpoints did not return clip data.", "WARN")
            self.log("  This is normal if Artlist uses client-side API calls instead.", "INFO")

        return clips

    # ── Phase 4: Clip ID probing ──────────────────────────────────────────────

    def _probe_clip_ids(self, build_id=''):
        """Probe clip IDs via _next/data JSON endpoints. Requires buildId."""
        clips = []
        import time

        if not build_id:
            self.log("  No buildId — cannot probe. Run browser bootstrap or API Discovery first.", "WARN")
            return clips

        # Determine ID range to probe
        try:
            row = self.db.execute(
                "SELECT MAX(CAST(clip_id AS INTEGER)) as max_id FROM clips WHERE clip_id != ''"
            ).fetchone()
            known_max = int(row['max_id'] or 0) if row else 0
        except Exception:
            known_max = 0

        # Seed IDs from web search results + DB
        seed_ids = [6590530, 6451306, 6374774, 6015761, 486409]
        max_known = max(seed_ids + [known_max])

        self.log(f"  buildId: {build_id}", "INFO")
        self.log(f"  DB max clip ID: {known_max}  |  Seed max: {max_known}", "INFO")

        # First: quick connectivity test with a known-good ID
        test_url = f'https://artlist.io/_next/data/{build_id}/stock-footage/clip/x/6451306.json'
        test_data = self._http_get_json(test_url, timeout=10)
        if not test_data:
            # BuildId may be stale — _next/data 404s when buildId changes on deploy
            self.log("  _next/data returned nothing for known clip — buildId may be stale.", "WARN")
            self.log("  Try running API Discovery or Direct HTTP again to refresh buildId.", "WARN")
            return clips

        test_clips = self._walk_for_clips(test_data)
        if test_clips:
            clips.extend(test_clips)
            self.log(f"  Connectivity test OK: got clip data for ID 6451306", "OK")
        else:
            self.log("  _next/data returned JSON but no clip data — endpoint may have changed.", "WARN")
            return clips

        # Probe strategy: scan downward from max known ID
        start_id = max_known + 500  # Check a bit above max in case new clips were added
        max_probes = self.cfg.get('max_pages', 500) or 500
        max_gap = 50       # consecutive misses before jumping
        jump_size = 5000   # how far to jump on a gap
        max_jumps = 20     # stop after this many jumps with no data
        min_id = 100000    # don't go below this

        self.log(f"  Probing {max_probes} IDs from {start_id} downward...", "INFO")

        probe_count = 0
        consecutive_miss = 0
        jump_count = 0
        current_id = start_id
        thumb_dir = os.path.join(
            os.environ.get('APPDATA', os.path.expanduser('~')),
            'ArtlistScraper', 'thumbnails')

        while probe_count < max_probes and not self._stop.is_set():
            if consecutive_miss >= max_gap:
                jump_count += 1
                if jump_count > max_jumps or current_id - jump_size < min_id:
                    self.log(f"  Reached probe limit after {jump_count} jumps", "INFO")
                    break
                current_id -= jump_size
                consecutive_miss = 0
                self.log(f"  Gap detected — jumping to ID {current_id}", "DEBUG")
                continue

            probe_count += 1
            url = f'https://artlist.io/_next/data/{build_id}/stock-footage/clip/x/{current_id}.json'
            data = self._http_get_json(url, timeout=8)

            if data:
                found = self._walk_for_clips(data)
                if found:
                    clips.extend(found)
                    consecutive_miss = 0
                    jump_count = max(0, jump_count - 1)  # Successful hits reduce jump count
                else:
                    consecutive_miss += 1
            else:
                consecutive_miss += 1

            current_id -= 1

            # Rate limiting: ~10 req/sec
            if probe_count % 10 == 0:
                time.sleep(0.5)

            # Progress + incremental save
            if probe_count % 100 == 0:
                self.log(
                    f"  Progress: {probe_count}/{max_probes} probed, {len(clips)} found, "
                    f"at ID {current_id}, gaps: {jump_count}", "INFO")
                # Save batch to DB
                for c in clips[-100:]:
                    c.setdefault('source_site', 'Artlist')
                    self._save_clip_meta(c)
                    if c.get('thumbnail_url') and c.get('clip_id'):
                        self._download_thumb(c['thumbnail_url'], c['clip_id'], thumb_dir)
                self.stats_signal.emit(self.db.stats())

        self.log(f"  ID probing done: {probe_count} probed, {len(clips)} clips found", "OK" if clips else "WARN")
        return clips

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
                p.setPen(QColor(C('warning')))
                p.setBrush(QBrush(QColor(C('warning'))))
            else:
                p.setPen(QColor(C('border_light')))
                p.setBrush(QBrush(QColor(C('border'))))
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

    # (card_width, thumb_height)  for size-index 0=S 1=M 2=L 3=XL
    SIZES = [(160, 90), (200, 112), (240, 135), (320, 180)]

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
        vlay.setContentsMargins(0,0,0,Z(8))
        vlay.setSpacing(0)

        # ── Thumbnail ─────────────────────────────────────────────────────
        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(cw, th)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setStyleSheet(
            f"background:{C('bg_video')}; border-radius:{Z(7)}px {Z(7)}px 0 0; border:none;")
        self._cw, self._th = cw, th
        self._set_placeholder()
        vlay.addWidget(self.thumb_label)

        # ── Content ───────────────────────────────────────────────────────
        content = QWidget()
        content.setStyleSheet("background:transparent;")
        clay = QVBoxLayout(content)
        clay.setContentsMargins(Z(8),Z(5),Z(8),0)
        clay.setSpacing(Z(2))

        # Title (2 lines max)
        title = _g('title') or self._clip_id
        self.title_lbl = QLabel(title)
        self.title_lbl.setWordWrap(True)
        self.title_lbl.setMaximumHeight(Z(34))
        self.title_lbl.setStyleSheet(
            f"color:{C('text')}; font-size:{Z(11)}px; font-weight:700; background:transparent;")
        self.title_lbl.setToolTip(title)
        clay.addWidget(self.title_lbl)

        # Creator
        creator = _g('creator')
        if creator:
            cl = QLabel(creator)
            cl.setStyleSheet(
                f"color:{C('warning')}; font-size:{Z(10)}px; background:transparent;")
            cl.setToolTip(creator)
            clay.addWidget(cl)

        # Badges row: resolution | duration | fps | fav heart | rating | status dot
        badges = QHBoxLayout(); badges.setSpacing(Z(3)); badges.setContentsMargins(0,Z(3),0,0)
        # Favorite heart
        if self._favorited:
            heart = QLabel('\u2665')
            heart.setStyleSheet(f"color:{C('error')}; font-size:{Z(10)}px; background:transparent;")
            heart.setToolTip("Favorited")
            badges.addWidget(heart)
        for txt, clr in [(_g('resolution'),C('purple')),(_g('duration'),C('accent')),(_g('frame_rate'),C('success'))]:
            if txt:
                b = QLabel(txt)
                b.setStyleSheet(
                    f"background:{clr}22; color:{clr}; font-size:{Z(8)}px; "
                    f"font-weight:700; padding:{Z(1)}px {Z(5)}px; border-radius:{Z(3)}px;")
                badges.addWidget(b)
        badges.addStretch()
        # Rating stars (compact)
        if self._user_rating > 0:
            stars_text = '\u2605' * self._user_rating
            stars = QLabel(stars_text)
            stars.setStyleSheet(f"color:{C('warning')}; font-size:{Z(8)}px; background:transparent;")
            badges.addWidget(stars)
        ds = self._dl_status
        dot_clr = {'done':C('success'),'downloading':C('warning'),'error':C('error')}.get(ds,C('border'))
        dot = QLabel('●')
        dot.setStyleSheet(f"color:{dot_clr}; font-size:{Z(10)}px; background:transparent;")
        dot.setToolTip({'done':'Downloaded','downloading':'Downloading...','error':'Error','':''}.get(ds,''))
        badges.addWidget(dot)
        clay.addLayout(badges)

        # Tag chips (first 5 tags)
        tags_raw = _g('tags')
        tags = [t.strip() for t in tags_raw.split(',') if t.strip()][:5]
        if tags:
            trow = QHBoxLayout(); trow.setSpacing(Z(3)); trow.setContentsMargins(0,Z(3),0,0)
            for t in tags:
                tb = QPushButton(t[:18])
                tb.setObjectName('tag-chip')
                tb.setFixedHeight(Z(16))
                tb.clicked.connect(lambda _=False, tag=t: self.tag_clicked.emit(tag))
                trow.addWidget(tb)
            trow.addStretch()
            clay.addLayout(trow)

        vlay.addWidget(content)

        # Defer thumb loading — store path for lazy load
        self._pending_thumb = None
        tp = _g('thumb_path')
        if tp and os.path.isfile(tp):
            self._pending_thumb = tp
        elif thumb_dir and self._clip_id:
            candidate = os.path.join(thumb_dir, f"{self._clip_id}.jpg")
            if os.path.isfile(candidate):
                self._pending_thumb = candidate

    def load_deferred_thumb(self):
        """Load the thumbnail if it was deferred during construction."""
        if self._pending_thumb:
            self.set_thumb(self._pending_thumb)
            self._pending_thumb = None

    def _set_placeholder(self):
        pm = QPixmap(self._cw, self._th)
        pm.fill(QColor(C('bg_deep')))
        # Draw a subtle film-frame icon
        painter = QPainter(pm)
        painter.setPen(QColor(C('border')))
        painter.drawRect(self._cw//2-16, self._th//2-12, 32, 24)
        painter.setPen(QColor(C('border_light')))
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
            canvas.fill(QColor(C('bg_deep')))
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
                self._hover_video.setStyleSheet(f"background:{C('bg_video')}; border-radius:{Z(7)}px {Z(7)}px 0 0;")
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



# ─────────────────────────────────────────────────────────────────────────────
# IMPORT WORKER — scan local folder and add videos to DB
# ─────────────────────────────────────────────────────────────────────────────

_VIDEO_EXTENSIONS = frozenset({
    '.mp4', '.webm', '.mkv', '.avi', '.mov', '.m4v', '.flv', '.wmv', '.ts', '.mts',
})

class ImportWorker(QThread):
    """Scans a folder for video files, extracts metadata via ffprobe, imports into DB."""
    log_signal      = pyqtSignal(str)
    progress_signal = pyqtSignal(int, int)       # current, total
    clip_signal     = pyqtSignal(dict)            # each imported clip
    finished        = pyqtSignal(int)             # total imported count

    def __init__(self, folder, db, thumb_dir, recursive=True):
        super().__init__()
        self.folder     = folder
        self.db         = db
        self.thumb_dir  = thumb_dir
        self.recursive  = recursive
        self._stop      = threading.Event()

    def stop(self): self._stop.set()

    def run(self):
        import hashlib
        ffmpeg = _get_ffmpeg()
        # Resolve ffprobe path from ffmpeg path
        ffprobe = self._find_ffprobe(ffmpeg)

        # Scan for video files
        video_files = []
        if self.recursive:
            for root, dirs, files in os.walk(self.folder):
                if self._stop.is_set(): break
                for f in files:
                    if os.path.splitext(f)[1].lower() in _VIDEO_EXTENSIONS:
                        video_files.append(os.path.join(root, f))
        else:
            for f in os.listdir(self.folder):
                fp = os.path.join(self.folder, f)
                if os.path.isfile(fp) and os.path.splitext(f)[1].lower() in _VIDEO_EXTENSIONS:
                    video_files.append(fp)

        total = len(video_files)
        self.log_signal.emit(f"Found {total} video files in {self.folder}")
        if total == 0:
            self.finished.emit(0)
            return

        imported = 0
        os.makedirs(self.thumb_dir, exist_ok=True)

        for i, fpath in enumerate(video_files):
            if self._stop.is_set(): break
            self.progress_signal.emit(i + 1, total)

            fname = os.path.basename(fpath)
            name_no_ext = os.path.splitext(fname)[0]
            ext = os.path.splitext(fname)[1].lower()

            # Generate a stable clip_id from the absolute path
            clip_id = 'local_' + hashlib.md5(os.path.abspath(fpath).encode()).hexdigest()[:12]

            # Check if already in DB (by clip_id or local_path)
            existing = self.db.execute(
                "SELECT clip_id FROM clips WHERE clip_id=? OR local_path=?",
                (clip_id, fpath)).fetchone()
            if existing:
                continue

            # Extract metadata via ffprobe
            meta = self._probe(ffprobe, fpath)

            # Clean title from filename
            title = name_no_ext.replace('_', ' ').replace('-', ' ').strip()

            clip_data = {
                'clip_id':      clip_id,
                'source_url':   '',
                'title':        title,
                'creator':      '',
                'collection':   os.path.basename(os.path.dirname(fpath)),
                'resolution':   meta.get('resolution', ''),
                'duration':     meta.get('duration', ''),
                'frame_rate':   meta.get('fps', ''),
                'camera':       '',
                'formats':      ext.lstrip('.').upper(),
                'tags':         '',
                'm3u8_url':     '',
                'thumbnail_url':'',
                'source_site':  'Local Import',
            }

            is_new = self.db.save_clip(clip_data)
            if is_new:
                # Set local_path and dl_status so it shows as downloaded
                self.db.update_local_path(clip_id, fpath, 'done')
                imported += 1
                self.clip_signal.emit(clip_data)

                # Generate thumbnail
                thumb_path = os.path.join(self.thumb_dir, f"{clip_id}.jpg")
                if not os.path.isfile(thumb_path):
                    self._extract_thumb(ffmpeg, fpath, thumb_path)
                if os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0:
                    self.db.update_thumb_path(clip_id, thumb_path)

                if imported % 25 == 0:
                    self.log_signal.emit(f"Imported {imported} / {i+1} scanned...")

        self.log_signal.emit(f"Import complete: {imported} new clips from {total} files")
        self.finished.emit(imported)

    def _find_ffprobe(self, ffmpeg_path):
        """Derive ffprobe path from ffmpeg path."""
        import shutil
        # Try sibling binary
        if ffmpeg_path and os.path.isfile(ffmpeg_path):
            d = os.path.dirname(ffmpeg_path)
            for name in ('ffprobe', 'ffprobe.exe'):
                candidate = os.path.join(d, name)
                if os.path.isfile(candidate):
                    return candidate
        return shutil.which('ffprobe') or 'ffprobe'

    def _probe(self, ffprobe, fpath):
        """Extract resolution, duration, fps from a video file."""
        meta = {}
        try:
            cmd = [
                ffprobe, '-v', 'quiet',
                '-print_format', 'json',
                '-show_format', '-show_streams',
                fpath
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return meta
            info = json.loads(r.stdout)

            # Find video stream
            for s in info.get('streams', []):
                if s.get('codec_type') == 'video':
                    w = s.get('width', 0)
                    h = s.get('height', 0)
                    if w and h:
                        meta['resolution'] = f"{w}x{h}"
                    # FPS
                    fps_str = s.get('r_frame_rate', '')
                    if fps_str and '/' in fps_str:
                        num, den = fps_str.split('/')
                        try:
                            fps_val = round(int(num) / int(den))
                            if 1 < fps_val < 999:
                                meta['fps'] = str(fps_val)
                        except (ValueError, ZeroDivisionError):
                            pass
                    break

            # Duration
            dur = info.get('format', {}).get('duration', '')
            if dur:
                try:
                    secs = float(dur)
                    mins = int(secs) // 60
                    sec_r = int(secs) % 60
                    meta['duration'] = f"{mins}:{sec_r:02d}"
                except ValueError:
                    pass
        except Exception:
            pass
        return meta

    def _extract_thumb(self, ffmpeg, video_path, out_path):
        """Extract a single thumbnail frame from a video file."""
        try:
            cmd = [ffmpeg, '-y',
                   '-ss', '3',
                   '-i', video_path,
                   '-frames:v', '1',
                   '-vf', 'thumbnail,scale=320:-1',
                   '-q:v', '3',
                   out_path]
            subprocess.run(cmd, capture_output=True, timeout=30)
        except Exception:
            pass


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
        """Download a single clip with smart retry — skips retries for permanent failures."""
        import time as _time
        clip_id = str(clip.get('clip_id', '') or '')

        for attempt in range(self.max_retries + 1):
            if self._stop.is_set():
                return
            if attempt > 0:
                wait = min(2 ** attempt, 15)  # cap at 15s (was 30)
                self.log(f"Retry {attempt}/{self.max_retries} for [{clip_id}] in {wait}s", "WARN")
                self.progress_signal.emit(clip_id, 0, f"Retry {attempt} in {wait}s...")
                _time.sleep(wait)
                if self._stop.is_set():
                    return

            result = self._download_one(clip, ffmpeg)
            if result == 'ok':
                return
            if result == 'permanent':
                # URL expired/dead/403/404 — retrying won't help
                self.db.set_dl_status(clip_id, 'error')
                self.log(f"Permanent failure [{clip_id}] — skipping retries", "ERROR")
                self.clip_done.emit(clip_id, False, "URL expired or unreachable")
                return

        # All retries exhausted (transient failures only reach here)
        self.db.set_dl_status(clip_id, 'error')
        self.progress_signal.emit(clip_id, 0, f"Failed after {self.max_retries+1} attempts")
        self.log(f"Gave up [{clip_id}] after {self.max_retries+1} attempts", "ERROR")
        self.clip_done.emit(clip_id, False, f"Failed after {self.max_retries+1} attempts")

    @staticmethod
    def _head_check_url(url, timeout=8):
        """Quick HTTP HEAD/GET check. Returns (ok, reason).
        Catches expired CDN URLs in ~1s instead of letting ffmpeg hang for minutes."""
        import urllib.request, urllib.error
        try:
            req = urllib.request.Request(url, method='HEAD')
            req.add_header('User-Agent', 'Mozilla/5.0')
            resp = urllib.request.urlopen(req, timeout=timeout)
            code = resp.getcode()
            resp.close()
            if code and code >= 400:
                return False, f"HTTP {code}"
            return True, "ok"
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}"
        except urllib.error.URLError as e:
            # Could be DNS or connection issue — might be transient
            reason = str(getattr(e, 'reason', e))
            if 'timed out' in reason.lower() or 'timeout' in reason.lower():
                return False, "timeout"
            return False, f"URL error: {reason[:60]}"
        except Exception as e:
            return False, f"check failed: {str(e)[:60]}"

    _FFMPEG_PROCESS_TIMEOUT = 180   # kill ffmpeg if no progress for 3 minutes

    def _download_one(self, clip, ffmpeg):
        """Download a single clip. Returns 'ok', 'permanent', or 'transient'."""
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
                        return 'ok'
                    # Use latest m3u8_url from DB (may have been upgraded to HD/UHD)
                    if check['m3u8_url']:
                        m3u8_url = check['m3u8_url']
            except Exception:
                pass

        # Poll DB until title is populated (metadata extraction runs after M3U8 fires).
        fresh = None
        if clip_id:
            for _attempt in range(12):          # 12 x 0.5s = 6s max wait (was 15s)
                if self._stop.is_set():
                    return 'transient'
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
            return 'permanent'

        # ── HTTP HEAD pre-check — catch expired URLs in ~1s ──────────
        self.progress_signal.emit(clip_id, 0, "Checking URL...")
        url_ok, url_reason = self._head_check_url(m3u8_url)
        if not url_ok:
            http_code = 0
            code_match = re.search(r'HTTP (\d+)', url_reason)
            if code_match:
                http_code = int(code_match.group(1))

            # 403/404/410/451 = expired CDN token / deleted — permanent
            if http_code in (403, 404, 410, 451):
                self.progress_signal.emit(clip_id, 0, f"URL expired ({url_reason})")
                self.log(f"[DL] SKIP id:{clip_id} — {url_reason} (permanent)", "WARN")
                return 'permanent'
            # Timeout or 5xx = maybe transient
            elif 'timeout' in url_reason.lower() or (500 <= http_code < 600):
                self.progress_signal.emit(clip_id, 0, f"URL unreachable ({url_reason})")
                self.log(f"[DL] URL check failed id:{clip_id} — {url_reason} (transient)", "WARN")
                return 'transient'
            else:
                # Unknown error — treat as permanent to avoid wasting time
                self.progress_signal.emit(clip_id, 0, f"URL dead ({url_reason})")
                self.log(f"[DL] SKIP id:{clip_id} — {url_reason} (permanent)", "WARN")
                return 'permanent'

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
            return 'ok'

        # Disk space check — require at least 500 MB free before starting a download
        try:
            usage = shutil.disk_usage(os.path.dirname(out_path) or self.out_dir)
            free_mb = usage.free / 1_048_576
            if free_mb < 500:
                self.log(f"[DL] SKIP id:{clip_id} — low disk space ({free_mb:.0f} MB free, need 500 MB)", "ERROR")
                self.progress_signal.emit(clip_id, 0, f"Low disk space ({free_mb:.0f} MB)")
                self.clip_done.emit(clip_id, False, f"Low disk space: {free_mb:.0f} MB free")
                return 'permanent'
        except Exception:
            pass  # Can't check disk space, proceed anyway

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
            # ffmpeg with aggressive timeouts:
            #   -rw_timeout 15000000  = 15s read/write timeout per TCP operation (microseconds)
            #   -reconnect 1          = auto-reconnect on connection drop
            #   -reconnect_streamed 1 = reconnect even for streamed content
            #   -reconnect_delay_max 5 = max 5s between reconnect attempts
            cmd = [
                ffmpeg, '-y',
                '-rw_timeout', '15000000',
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '5',
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
            last_progress_time = dl_start_time  # watchdog: last time we saw progress

            for line in proc.stderr:
                if self._stop.is_set():
                    proc.terminate(); break
                line = line.strip()

                # Watchdog: kill if no progress output for _FFMPEG_PROCESS_TIMEOUT
                now = _time.time()
                if line:
                    last_progress_time = now
                if now - last_progress_time > self._FFMPEG_PROCESS_TIMEOUT:
                    self.log(f"[DL] WATCHDOG: killing hung ffmpeg for [{clip_id}] (no output for {self._FFMPEG_PROCESS_TIMEOUT}s)", "ERROR")
                    proc.kill()
                    break

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

            proc.wait(timeout=30)
            rc = proc.returncode

            with self._procs_lock:
                self._procs.pop(clip_id, None)

            if self._stop.is_set():
                return 'transient'

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
                return 'ok'
            else:
                # Clean up partial file
                try:
                    if os.path.exists(out_path) and os.path.getsize(out_path) == 0:
                        os.remove(out_path)
                except Exception:
                    pass
                err = f"ffmpeg exit {rc}"
                self.progress_signal.emit(clip_id, 0, f"Error (exit {rc})")
                self.log(f"Failed [{clip_id}]: {err}", "ERROR")
                return 'transient'

        except subprocess.TimeoutExpired:
            # proc.wait(timeout=30) expired — kill it
            try: proc.kill()
            except Exception: pass
            with self._procs_lock:
                self._procs.pop(clip_id, None)
            self.log(f"[DL] TIMEOUT: ffmpeg hung for [{clip_id}], killed", "ERROR")
            self.progress_signal.emit(clip_id, 0, "Timeout — ffmpeg killed")
            return 'transient'

        except Exception as e:
            with self._procs_lock:
                self._procs.pop(clip_id, None)
            err = str(e)
            self.progress_signal.emit(clip_id, 0, f"Error: {err[:60]}")
            self.log(f"Download error [{clip_id}]: {err}", "ERROR")
            return 'transient'

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
            print(f"[DL WARN] Sidecar write failed for {data.get('clip_id','?')}: {e}")


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
            'info':    (C('accent_hover'), C('toast_info_bg')),
            'success': (C('success'), C('toast_success_bg')),
            'warning': (C('warning'), C('toast_warning_bg')),
            'error':   (C('error'), C('toast_error_bg')),
        }
        fg, bg = colors.get(level, colors['info'])
        self.setStyleSheet(
            f"background:{bg}; color:{fg}; border:1px solid {fg}40; "
            f"border-radius:{Z(6)}px; padding:{Z(10)}px {Z(20)}px; font-size:{Z(12)}px; font-weight:600;")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.adjustSize()
        self.setFixedWidth(max(self.width() + Z(40), Z(300)))
        # Position: top-center of parent
        px = (parent.width() - self.width()) // 2
        self.move(px, Z(60))
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

        self.setWindowTitle("Video Scraper  v1.4.0")
        self.setMinimumSize(Z(960), Z(600))
        self.resize(Z(1400), Z(860))

        self._init_db()
        self._build_ui()
        self._load_saved_config()
        self._check_browser_status()

        self._dl_clip_rows = {}   # populated by _start_downloads
        self._dl_done_count = 0
        self._last_rows = []
        self._thumb_worker = None
        self._import_worker = None
        self._load_more_btn = None
        self._bg_workers = []     # prevent GC of background QThread workers
        self._active_profile = SiteProfile.get('Artlist')
        self._catalog_mode = False
        self._selected_cards = []  # multi-select: list of (card, row) tuples
        self._last_click_idx = -1  # for Shift+click range selection

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

        # Periodic WAL checkpoint to prevent unbounded WAL growth
        self._wal_timer = QTimer()
        self._wal_timer.timeout.connect(lambda: self.db.wal_checkpoint() if self.db else None)
        self._wal_timer.start(120_000)  # every 2 minutes

        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._do_search_impl)

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
        QShortcut(QKeySequence("Ctrl+="), self, activated=self._zoom_in)
        QShortcut(QKeySequence("Ctrl++"), self, activated=self._zoom_in)
        QShortcut(QKeySequence("Ctrl+-"), self, activated=self._zoom_out)
        QShortcut(QKeySequence("Ctrl+0"), self, activated=self._zoom_reset)
        QShortcut(QKeySequence("Ctrl+A"), self, activated=self._select_all_cards)
        QShortcut(QKeySequence("Escape"), self, activated=self._deselect_all_cards)

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
        pm.fill(QColor(C('accent')))
        p = QPainter(pm)
        p.setPen(QColor('#ffffff'))
        p.setFont(QFont('Segoe UI', 16, QFont.Weight.Bold))
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, 'V')
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
                "Video Scraper", "Running in background. Double-click tray to restore.",
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

    # ── Zoom System ────────────────────────────────────────────────────────

    def _on_zoom_changed(self, idx):
        """Handle zoom combo selection."""
        scale = self._zoom_combo.currentData()
        if scale and abs(scale - _ui_scale) > 0.01:
            self._apply_zoom(scale)

    def _on_theme_changed(self, theme_name):
        """Handle theme combo selection."""
        if theme_name and theme_name in THEME_PALETTES:
            _set_theme(theme_name)
            cfg = load_config() or {}
            cfg['theme'] = theme_name
            save_config(cfg)
            self._apply_zoom(_ui_scale)

    def _zoom_in(self):
        idx = self._zoom_combo.currentIndex()
        if idx < self._zoom_combo.count() - 1:
            self._zoom_combo.setCurrentIndex(idx + 1)

    def _zoom_out(self):
        idx = self._zoom_combo.currentIndex()
        if idx > 0:
            self._zoom_combo.setCurrentIndex(idx - 1)

    def _zoom_reset(self):
        for i in range(self._zoom_combo.count()):
            if self._zoom_combo.itemData(i) == 1.0:
                self._zoom_combo.setCurrentIndex(i)
                break

    def _apply_zoom(self, scale):
        """Regenerate stylesheet, rebuild entire UI at new scale."""
        global _ui_scale
        _ui_scale = scale

        # Update ClipCard size table
        sf = scale * _dpi_factor
        ClipCard.SIZES = [
            (int(160 * sf), int(90 * sf)),
            (int(200 * sf), int(112 * sf)),
            (int(240 * sf), int(135 * sf)),
            (int(320 * sf), int(180 * sf)),
        ]

        # Get current theme
        cfg = load_config() or {}
        theme_name = cfg.get('theme', 'OLED')

        # Apply themed + scaled stylesheet
        QApplication.instance().setStyleSheet(_build_stylesheet(scale, theme_name))

        # Full UI rebuild to re-evaluate all Z() and C() calls
        current_tab = self.tabs.currentIndex() if hasattr(self, 'tabs') else 0
        old_central = self.centralWidget()
        self._build_ui()
        if old_central:
            old_central.deleteLater()
        self.tabs.setCurrentIndex(current_tab)

        # Restore state after rebuild
        self._load_saved_config()
        self._do_search()
        self._update_stats()
        self._refresh_filter_dropdowns()
        self._refresh_collections_combo()
        self._refresh_saved_searches()

        # Re-sync zoom combo (rebuild created a new one)
        for i in range(self._zoom_combo.count()):
            if abs(self._zoom_combo.itemData(i) - scale) < 0.01:
                self._zoom_combo.blockSignals(True)
                self._zoom_combo.setCurrentIndex(i)
                self._zoom_combo.blockSignals(False)
                break

        # Re-sync theme combo
        if hasattr(self, '_theme_combo'):
            idx_t = self._theme_combo.findText(theme_name)
            if idx_t >= 0:
                self._theme_combo.blockSignals(True)
                self._theme_combo.setCurrentIndex(idx_t)
                self._theme_combo.blockSignals(False)

        self._toast(f"Zoom: {int(scale * 100)}%", 'info', 1500)

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
        hdr.setObjectName('app-header')
        hdr.setFixedHeight(Z(48))
        hdr.setStyleSheet(f"background:{C('bg_deep')}; border-bottom:1px solid {C('border_subtle')};")
        lay = QHBoxLayout(hdr)
        lay.setContentsMargins(Z(20),0,Z(20),0)

        t = QLabel("VIDEO SCRAPER")
        t.setStyleSheet(f"font-size:{Z(13)}px; font-weight:700; color:{C('text')}; background:transparent; letter-spacing:3px;")
        lay.addWidget(t)
        lay.addStretch()

        self.lbl_clips_hdr  = self._hdr_lbl("Clips: 0",  "{C('text')}")
        self.lbl_m3u8_hdr   = self._hdr_lbl("M3U8: 0",   "{C('success')}")
        self.lbl_status_hdr = self._hdr_lbl("● Idle",    "{C('text_muted')}")

        for lbl in (self.lbl_clips_hdr, self.lbl_m3u8_hdr, self.lbl_status_hdr):
            lay.addSpacing(Z(22)); lay.addWidget(lbl)

        # ── Zoom controls ──────────────────────────────────────────────────
        lay.addSpacing(Z(16))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet(f"color:{C('border')};"); sep.setFixedHeight(Z(24))
        lay.addWidget(sep); lay.addSpacing(Z(8))

        zoom_lbl = QLabel("Zoom:")
        zoom_lbl.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(11)}px; font-weight:600; background:transparent;")
        lay.addWidget(zoom_lbl); lay.addSpacing(Z(4))

        self._zoom_combo = QComboBox()
        self._zoom_combo.setFixedWidth(Z(80))
        self._zoom_combo.setStyleSheet(
            f"QComboBox {{ background:{C('accent_combo')}; color:{C('text')}; border:1px solid {C('border_input')}; "
            f"border-radius:{Z(4)}px; padding:{Z(2)}px {Z(6)}px; font-size:{Z(11)}px; font-weight:600; }}"
            f"QComboBox QAbstractItemView {{ font-size:{Z(11)}px; }}")
        for label, val in ZOOM_PRESETS:
            self._zoom_combo.addItem(label, val)
        # Set to current zoom level
        for idx in range(self._zoom_combo.count()):
            if abs(self._zoom_combo.itemData(idx) - _ui_scale) < 0.01:
                self._zoom_combo.setCurrentIndex(idx)
                break
        self._zoom_combo.currentIndexChanged.connect(self._on_zoom_changed)
        lay.addWidget(self._zoom_combo)

        # ── Theme selector ──────────────────────────────────────────────────
        lay.addSpacing(Z(8))
        theme_lbl = QLabel("Theme:")
        theme_lbl.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(11)}px; font-weight:600; background:transparent;")
        lay.addWidget(theme_lbl); lay.addSpacing(Z(4))

        self._theme_combo = QComboBox()
        self._theme_combo.setFixedWidth(Z(100))
        self._theme_combo.setStyleSheet(
            f"QComboBox {{ background:{C('accent_combo')}; color:{C('text')}; border:1px solid {C('border_input')}; "
            f"border-radius:{Z(4)}px; padding:{Z(2)}px {Z(6)}px; font-size:{Z(11)}px; font-weight:600; }}"
            f"QComboBox QAbstractItemView {{ font-size:{Z(11)}px; }}")
        for name in THEME_NAMES:
            self._theme_combo.addItem(name)
        # Restore saved theme
        cfg = load_config()
        saved_theme = cfg.get('theme', 'OLED')
        idx_t = self._theme_combo.findText(saved_theme)
        if idx_t >= 0:
            self._theme_combo.setCurrentIndex(idx_t)
        self._theme_combo.currentTextChanged.connect(self._on_theme_changed)
        lay.addWidget(self._theme_combo)

        return hdr

    def _hdr_lbl(self, text, color):
        l = QLabel(text)
        l.setStyleSheet(f"font-size:{Z(12)}px; font-weight:600; background:transparent; color:{color};")
        return l

    # ── Config Tab ──────────────────────────────────────────────────────────

    def _build_config_tab(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget(); scroll.setWidget(inner)
        lay = QVBoxLayout(inner); lay.setContentsMargins(Z(24),Z(20),Z(24),Z(24)); lay.setSpacing(Z(14))

        # Site Profiles — multi-select for round-robin rotation
        grp_profile = QGroupBox("Site Profiles  (round-robin rotation)"); gp = QVBoxLayout(grp_profile)
        self._profile_checks = {}
        for name in SiteProfile.all_names():
            prof = SiteProfile.get(name)
            chk = QCheckBox(f"{name}  --  {prof.description[:55]}")
            chk.setChecked(name in ('Artlist', 'Pexels'))
            chk.setStyleSheet(f"color:{C('text_soft')}; font-size:{Z(12)}px;")
            gp.addWidget(chk)
            self._profile_checks[name] = chk
        brow = QHBoxLayout()
        brow.addWidget(QLabel("Batch size per site:"))
        self.spin_batch_size = QSpinBox(); self.spin_batch_size.setRange(5, 5000)
        self.spin_batch_size.setValue(100); self.spin_batch_size.setSuffix(" pages")
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
        self.inp_url.setMinimumWidth(Z(200)); r.addWidget(self.inp_url, 1); g.addLayout(r)
        g.addWidget(self._sub("Crawler follows all video links from this page automatically."))
        lay.addWidget(grp)

        # Rate limits
        grp2 = QGroupBox("Rate Limiting  (Server Safety)"); g2 = QVBoxLayout(grp2)
        def spinrow(lbl, attr, mn, mx, val, sfx, tip):
            r2 = QHBoxLayout(); l = QLabel(lbl); l.setFixedWidth(Z(140)); r2.addWidget(l)
            sp = QSpinBox(); sp.setRange(mn,mx); sp.setValue(val)
            sp.setSuffix(sfx); sp.setFixedWidth(Z(130)); setattr(self, attr, sp)
            r2.addWidget(sp); r2.addSpacing(Z(10))
            t2 = QLabel(tip); t2.setObjectName("subtext"); r2.addWidget(t2,1); return r2
        g2.addLayout(spinrow("Page delay:",   'spin_page_delay',   200,30000,2000," ms","Wait between page loads."))
        g2.addLayout(spinrow("Scroll delay:", 'spin_scroll_delay', 50, 5000, 500, " ms","Wait between scroll steps."))
        g2.addLayout(spinrow("M3U8 wait:",    'spin_m3u8_wait',   500,15000,3000," ms","Dwell after load for player to fire M3U8 requests."))
        g2.addLayout(spinrow("Scroll steps:", 'spin_scroll_steps',   3,  200,  20,   "","Scroll passes on catalog pages."))
        g2.addLayout(spinrow("Page timeout:", 'spin_timeout',     5000,120000,30000," ms","Max load wait before skip."))
        lay.addWidget(grp2)

        # Limits
        grp3 = QGroupBox("Crawl Limits  (0 = unlimited)"); g3 = QVBoxLayout(grp3)
        g3.addLayout(spinrow("Max pages:", 'spin_max_pages', 0,99999,0,"","Stop after N pages (0=unlimited)."))
        g3.addLayout(spinrow("Max depth:", 'spin_max_depth', 1,   25,3,"","Depth from start URL."))
        lay.addWidget(grp3)

        # Options
        grp4 = QGroupBox("Options"); g4 = QHBoxLayout(grp4)
        self.chk_headless = QCheckBox("Headless mode"); self.chk_headless.setChecked(True)
        self.chk_headless.setToolTip("Uncheck to see the browser — required for solving CAPTCHAs/challenges")
        self.chk_resume   = QCheckBox("Resume mode  (skip already-crawled pages)"); self.chk_resume.setChecked(True)
        g4.addWidget(self.chk_headless); g4.addSpacing(Z(30)); g4.addWidget(self.chk_resume); g4.addStretch()
        lay.addWidget(grp4)

        # Output dir
        grp5 = QGroupBox("Output Directory"); g5 = QHBoxLayout(grp5)
        self.inp_output = QLineEdit(os.path.join(os.path.expanduser('~'), 'ArtlistScraper', 'output'))
        g5.addWidget(self.inp_output,1)
        bb = QPushButton("Browse..."); bb.setObjectName("neutral"); bb.setFixedWidth(Z(90))
        bb.clicked.connect(self._browse_output); g5.addWidget(bb)
        lay.addWidget(grp5)

        btns = QHBoxLayout()
        sb = QPushButton("💾  Save Config"); sb.clicked.connect(self._save_cfg); sb.setFixedHeight(Z(36))
        lb = QPushButton("📂  Load Config"); lb.setObjectName("neutral"); lb.clicked.connect(self._load_cfg_file); lb.setFixedHeight(Z(36))
        btns.addWidget(sb); btns.addWidget(lb); btns.addStretch()
        lay.addLayout(btns); lay.addStretch()
        return scroll

    # ── Crawl Tab ───────────────────────────────────────────────────────────

    def _build_crawl_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(Z(20),Z(16),Z(20),Z(16)); lay.setSpacing(Z(12))

        # Stat cards
        cards = QHBoxLayout()
        for attr, label, color in [
            ('stat_clips',  'Clips Found',  C('accent')),
            ('stat_m3u8',   'With M3U8',    C('success')),
            ('stat_pages',  'Pages Done',   C('purple')),
            ('stat_queued', 'In Queue',     C('warning')),
            ('stat_errors', 'Errors',       C('error')),
        ]:
            card = QFrame(); card.setObjectName('stat-card'); card.setFixedHeight(Z(76))
            cl = QVBoxLayout(card); cl.setContentsMargins(Z(14),Z(8),Z(14),Z(8)); cl.setSpacing(Z(2))
            lv = QLabel("0")
            lv.setStyleSheet(f"font-size:{Z(24)}px; font-weight:700; color:{color}; background:transparent;")
            lv.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ll = QLabel(label); ll.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(11)}px; background:transparent;")
            ll.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cl.addWidget(lv); cl.addWidget(ll); setattr(self, attr, lv); cards.addWidget(card)
        lay.addLayout(cards)

        # ── Crawl Mode selector ─────────────────────────────────────────
        mode_row = QHBoxLayout(); mode_row.setSpacing(Z(8))
        mode_lbl = QLabel("Crawl Mode:")
        mode_lbl.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(11)}px; font-weight:600; background:transparent;")
        mode_row.addWidget(mode_lbl)

        self.combo_crawl_mode = QComboBox()
        self.combo_crawl_mode.setFixedHeight(Z(30))
        self.combo_crawl_mode.setMinimumWidth(Z(220))
        self.combo_crawl_mode.addItem("Full Crawl  (metadata + M3U8)", "full")
        self.combo_crawl_mode.addItem("Catalog Sweep  (fast metadata only)", "catalog_sweep")
        self.combo_crawl_mode.addItem("M3U8 Harvest  (enrich existing clips)", "m3u8_only")
        self.combo_crawl_mode.addItem("API Discovery  (find endpoints)", "api_discover")
        self.combo_crawl_mode.addItem("Direct HTTP  (no browser, fastest)", "direct_http")
        self.combo_crawl_mode.setToolTip(
            "Full Crawl: visits every clip page for complete data + M3U8 streams.\n"
            "Catalog Sweep: only browses listing pages — bulk-extracts from card grids.\n"
            "M3U8 Harvest: visits clips already in DB that are missing M3U8 URLs.\n"
            "API Discovery: one-time browser session to find Artlist's internal API endpoints.\n"
            "Direct HTTP: fastest — no browser at all. Uses sitemaps, __NEXT_DATA__,\n"
            "  _next/data endpoints, and clip ID probing for bulk metadata.")
        mode_row.addWidget(self.combo_crawl_mode)
        mode_row.addStretch()
        lay.addLayout(mode_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0,0); self.progress_bar.setFixedHeight(Z(5))
        self.progress_bar.setVisible(False); lay.addWidget(self.progress_bar)

        # ── Browser status banner ──────────────────────────────────────────
        self.browser_banner = QFrame()
        self.browser_banner.setStyleSheet(
            f"background:{C('toast_error_bg')}; border:1px solid {C('error')}40; border-radius:{Z(6)}px; padding:{Z(8)}px;")
        bb_lay = QHBoxLayout(self.browser_banner)
        bb_lay.setContentsMargins(Z(12),Z(6),Z(12),Z(6))
        self.lbl_browser_status = QLabel("⚠  Chromium browser not found — click Install to set it up.")
        self.lbl_browser_status.setStyleSheet(f"color:{C('error')}; font-weight:500;")
        bb_lay.addWidget(self.lbl_browser_status, 1)
        self.btn_install_browser = QPushButton("Install Browser")
        self.btn_install_browser.setObjectName("danger")
        self.btn_install_browser.setFixedHeight(Z(30)); self.btn_install_browser.setFixedWidth(Z(140))
        self.btn_install_browser.clicked.connect(self._install_browser)
        bb_lay.addWidget(self.btn_install_browser)
        lay.addWidget(self.browser_banner)
        btns = QHBoxLayout()
        self.btn_start = QPushButton("▶  Start Crawl")
        self.btn_start.setObjectName("success"); self.btn_start.setFixedHeight(Z(40))
        self.btn_start.clicked.connect(self._start_crawl)

        self.btn_pause = QPushButton("⏸  Pause")
        self.btn_pause.setObjectName("warning"); self.btn_pause.setFixedHeight(Z(40))
        self.btn_pause.setEnabled(False); self.btn_pause.clicked.connect(self._toggle_pause)

        self.btn_stop = QPushButton("⏹  Stop")
        self.btn_stop.setObjectName("danger"); self.btn_stop.setFixedHeight(Z(40))
        self.btn_stop.setEnabled(False); self.btn_stop.clicked.connect(self._stop_crawl)

        clrdb  = QPushButton("🗑  Clear DB");   clrdb.setObjectName("neutral");  clrdb.setFixedHeight(Z(40));  clrdb.clicked.connect(self._clear_db)
        rebuild_fts = QPushButton("🔄  Rebuild Index"); rebuild_fts.setObjectName("neutral"); rebuild_fts.setFixedHeight(Z(40)); rebuild_fts.setToolTip("Rebuild full-text search index if search results seem wrong"); rebuild_fts.clicked.connect(self._rebuild_fts)
        clrlog = QPushButton("Clear Log"); clrlog.setObjectName("neutral"); clrlog.setFixedHeight(Z(40)); clrlog.clicked.connect(lambda: self.log_view.clear())
        self.chk_verbose_log = QCheckBox("Verbose")
        self.chk_verbose_log.setChecked(True)
        self.chk_verbose_log.setToolTip("Show DEBUG-level log messages (detailed troubleshooting output)")
        self.chk_verbose_log.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(11)}px;")

        for b in (self.btn_start, self.btn_pause, self.btn_stop): btns.addWidget(b)
        btns.addStretch(); btns.addWidget(self.chk_verbose_log); btns.addWidget(rebuild_fts); btns.addWidget(clrdb); btns.addWidget(clrlog)
        lay.addLayout(btns)

        log_lbl = QLabel("Live Log"); log_lbl.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(11)}px; font-weight:600;")
        lay.addWidget(log_lbl)
        self.log_view = QTextEdit(); self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        lay.addWidget(self.log_view, 1)
        return w

    # ── Search Tab ──────────────────────────────────────────────────────────

    def _build_search_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(Z(20),Z(16),Z(20),Z(12)); lay.setSpacing(Z(8))

        # ── Search bar + view controls ────────────────────────────────────
        srow = QHBoxLayout(); srow.setSpacing(Z(6))
        self.inp_search = QLineEdit()
        self.inp_search.setPlaceholderText(
            "Search by title, tags, creator, collection, camera, resolution...")
        self.inp_search.setMinimumHeight(Z(38))
        self.inp_search.returnPressed.connect(lambda: (self._search_timer.stop(), self._do_search()))
        self.inp_search.textChanged.connect(lambda: self._search_timer.start(350))
        srow.addWidget(self.inp_search, 1)

        sb = QPushButton("Search"); sb.setFixedHeight(Z(38)); sb.setFixedWidth(Z(80))
        sb.clicked.connect(self._do_search); srow.addWidget(sb)

        # Catalog mode toggle
        self.btn_catalog = QPushButton("Catalog"); self.btn_catalog.setFixedHeight(Z(38))
        self.btn_catalog.setFixedWidth(Z(80)); self.btn_catalog.setCheckable(True)
        self.btn_catalog.setObjectName("neutral")
        self.btn_catalog.setToolTip("Catalog mode — full-width grid, all clips, sortable")
        self.btn_catalog.clicked.connect(self._toggle_catalog_mode)
        srow.addWidget(self.btn_catalog)

        # Sort dropdown (prominent in catalog mode, always functional)
        self.combo_sort = QComboBox(); self.combo_sort.setFixedHeight(Z(38))
        self.combo_sort.setMinimumWidth(Z(130))
        for label, key in [('Newest First','newest'),('Oldest First','oldest'),
                           ('Title A-Z','title_az'),('Title Z-A','title_za'),
                           ('Resolution','resolution'),('Shortest','duration_short'),
                           ('Longest','duration_long'),('Rating','rating')]:
            self.combo_sort.addItem(label, key)
        self.combo_sort.currentIndexChanged.connect(self._do_search)
        srow.addWidget(self.combo_sort)
        lay.addLayout(srow)

        # ── Filter row 1: column filters (wrapped for catalog hide/show) ──
        self._filter_row1 = QWidget()
        frow = QHBoxLayout(self._filter_row1); frow.setSpacing(Z(4)); frow.setContentsMargins(0,0,0,0)
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
            lbl.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(11)}px;")
            frow.addWidget(lbl)
            cb = QComboBox(); cb.setMinimumWidth(Z(70)); cb.setMaximumWidth(Z(180))
            cb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            cb.addItem("All"); setattr(self, attr, cb)
            self._filter_map[attr] = col
            cb.currentTextChanged.connect(self._do_search)
            frow.addWidget(cb)

        rfbtn = QPushButton("↻"); rfbtn.setObjectName("neutral")
        rfbtn.setFixedSize(Z(28), Z(28)); rfbtn.setToolTip("Refresh filters")
        rfbtn.clicked.connect(self._refresh_filter_dropdowns); frow.addWidget(rfbtn)
        frow.addStretch()
        lay.addWidget(self._filter_row1)

        # ── Filter row 2: asset management — scrollable to prevent cutoff ─
        self._filter_row2 = frow2_scroll = QScrollArea()
        frow2_scroll.setWidgetResizable(True)
        frow2_scroll.setFrameShape(QFrame.Shape.NoFrame)
        frow2_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        frow2_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        frow2_scroll.setFixedHeight(Z(44))
        frow2_scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")

        frow2_w = QWidget(); frow2_w.setStyleSheet("background:transparent;")
        frow2 = QHBoxLayout(frow2_w); frow2.setSpacing(Z(6)); frow2.setContentsMargins(0,Z(2),0,Z(2))

        # Duration range
        lbl_d = QLabel("Dur:"); lbl_d.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(11)}px;")
        frow2.addWidget(lbl_d)
        self.combo_duration = QComboBox(); self.combo_duration.setMinimumWidth(Z(65))
        for d in ['All', '0-10s', '10-30s', '30s-1m', '1-5m', '5m+']:
            self.combo_duration.addItem(d)
        self.combo_duration.currentTextChanged.connect(self._do_search)
        frow2.addWidget(self.combo_duration)

        # Collection filter
        lbl_c = QLabel("Coll:"); lbl_c.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(11)}px;")
        frow2.addWidget(lbl_c)
        self.combo_user_collection = QComboBox(); self.combo_user_collection.setMinimumWidth(Z(90))
        self.combo_user_collection.addItem("All")
        self.combo_user_collection.currentTextChanged.connect(self._do_search)
        frow2.addWidget(self.combo_user_collection)

        # Vertical separator
        sep1 = QFrame(); sep1.setFrameShape(QFrame.Shape.VLine)
        sep1.setStyleSheet(f"color:{C('border')};"); sep1.setFixedWidth(Z(1))
        frow2.addWidget(sep1)

        # Favorites toggle
        self.chk_favorites = QCheckBox("\u2665 Fav")
        self.chk_favorites.setStyleSheet(f"color:{C('error')}; font-size:{Z(11)}px; font-weight:500;")
        self.chk_favorites.toggled.connect(self._do_search)
        frow2.addWidget(self.chk_favorites)

        # Downloaded toggle
        self.chk_downloaded = QCheckBox("\u2713 DL'd")
        self.chk_downloaded.setStyleSheet(f"color:{C('success')}; font-size:{Z(11)}px; font-weight:500;")
        self.chk_downloaded.toggled.connect(self._do_search)
        frow2.addWidget(self.chk_downloaded)

        # AND/OR toggle
        self.btn_search_mode = QPushButton("OR")
        self.btn_search_mode.setObjectName("neutral"); self.btn_search_mode.setFixedSize(Z(36), Z(26))
        self.btn_search_mode.setCheckable(True); self.btn_search_mode.setToolTip("Toggle AND/OR search mode")
        self.btn_search_mode.clicked.connect(self._toggle_search_mode)
        frow2.addWidget(self.btn_search_mode)

        # Min rating filter
        lbl_mr = QLabel("\u2605:"); lbl_mr.setStyleSheet(f"color:{C('warning')}; font-size:{Z(11)}px;")
        frow2.addWidget(lbl_mr)
        self.spin_min_rating = QSpinBox(); self.spin_min_rating.setRange(0, 5)
        self.spin_min_rating.setValue(0); self.spin_min_rating.setFixedWidth(Z(44))
        self.spin_min_rating.setToolTip("Minimum star rating")
        self.spin_min_rating.valueChanged.connect(self._do_search)
        frow2.addWidget(self.spin_min_rating)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setStyleSheet(f"color:{C('border')};"); sep2.setFixedWidth(Z(1))
        frow2.addWidget(sep2)

        # Saved searches
        self.combo_saved_search = QComboBox(); self.combo_saved_search.setMinimumWidth(Z(120))
        self.combo_saved_search.addItem("Saved Searches...")
        self.combo_saved_search.activated.connect(self._load_saved_search)
        frow2.addWidget(self.combo_saved_search)

        btn_save_search = QPushButton("Save"); btn_save_search.setObjectName("neutral")
        btn_save_search.setFixedSize(Z(44), Z(26)); btn_save_search.setToolTip("Save current search as preset")
        btn_save_search.clicked.connect(self._save_current_search)
        frow2.addWidget(btn_save_search)

        sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.VLine)
        sep3.setStyleSheet(f"color:{C('border')};"); sep3.setFixedWidth(Z(1))
        frow2.addWidget(sep3)

        # Card size slider (only visible in card mode)
        self.lbl_card_size = QLabel("Size:")
        self.lbl_card_size.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(11)}px;")
        self.card_size_slider = QSlider(Qt.Orientation.Horizontal)
        self.card_size_slider.setRange(0, 3); self.card_size_slider.setValue(1)
        self.card_size_slider.setFixedWidth(Z(70))
        self.card_size_slider.valueChanged.connect(self._on_card_size_changed)
        frow2.addWidget(self.lbl_card_size); frow2.addWidget(self.card_size_slider)

        clrbtn = QPushButton("Clear"); clrbtn.setObjectName("neutral"); clrbtn.setFixedSize(Z(48), Z(26))
        clrbtn.clicked.connect(self._clear_search); frow2.addWidget(clrbtn)

        frow2_scroll.setWidget(frow2_w)
        lay.addWidget(frow2_scroll)

        # ── Main area: results splitter (left=cards, right=detail panel) ──
        self._search_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._search_splitter.setHandleWidth(Z(4))
        self._search_splitter.setStyleSheet(f"QSplitter::handle {{ background:{C('border_subtle')}; }}")
        lay.addWidget(self._search_splitter, 1)

        # ── Card grid (only view) ────────────────────────────────────────
        self._card_scroll = QScrollArea()
        self._card_scroll.setWidgetResizable(True)
        self._card_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._card_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._card_scroll.setStyleSheet(f"QScrollArea {{ background:{C('bg_card_area')}; }}")
        self._card_container = QWidget()
        self._card_container.setStyleSheet(f"background:{C('bg_card_area')};")
        self._card_flow = FlowLayout(self._card_container, h_spacing=Z(10), v_spacing=Z(10))
        self._card_flow.setContentsMargins(Z(12),Z(12),Z(12),Z(12))
        self._card_container.setLayout(self._card_flow)
        self._card_scroll.setWidget(self._card_container)
        self._search_splitter.addWidget(self._card_scroll)

        self._current_cards = []   # list of ClipCard widgets
        self._selected_card = None  # currently selected card

        # ── Detail panel (right side, always visible) ─────────────────────
        self._detail_panel = self._build_detail_panel()
        self._search_splitter.addWidget(self._detail_panel)
        self._detail_panel.setVisible(True)
        self._search_splitter.setSizes([Z(700), Z(360)])
        self._search_splitter.setCollapsible(1, False)

        # ── Bottom row ────────────────────────────────────────────────────
        brow = QHBoxLayout()
        self.lbl_result_count = QLabel("0 results"); self.lbl_result_count.setObjectName("subtext")
        brow.addWidget(self.lbl_result_count)
        self.lbl_import_status = QLabel(""); self.lbl_import_status.setObjectName("subtext")
        brow.addWidget(self.lbl_import_status)
        brow.addStretch()
        self.btn_import_folder = QPushButton("Import Folder")
        self.btn_import_folder.setObjectName("warning"); self.btn_import_folder.setFixedHeight(Z(30))
        self.btn_import_folder.setToolTip("Scan a local folder for video files and add them to the catalog")
        self.btn_import_folder.clicked.connect(self._import_folder)
        brow.addWidget(self.btn_import_folder)
        self.btn_fetch_thumbs = QPushButton("Fetch Thumbnails")
        self.btn_fetch_thumbs.setObjectName("neutral"); self.btn_fetch_thumbs.setFixedHeight(Z(30))
        self.btn_fetch_thumbs.clicked.connect(self._start_thumb_worker)
        brow.addWidget(self.btn_fetch_thumbs)
        lay.addLayout(brow)
        return w

    def _on_card_size_changed(self, val):
        self._populate_cards(self._last_rows if hasattr(self, '_last_rows') else [])

    def _toggle_catalog_mode(self):
        """Toggle catalog mode — maximize card grid for browsing all clips."""
        on = self.btn_catalog.isChecked()
        self._catalog_mode = on
        # Hide/show filter rows
        self._filter_row1.setVisible(not on)
        self._filter_row2.setVisible(not on)
        # Show/hide detail close button
        self._detail_close_btn.setVisible(on)
        # Collapse/restore detail panel
        if on:
            self._pre_catalog_sizes = self._search_splitter.sizes()
            self._search_splitter.setSizes([self._search_splitter.width(), 0])
            self._detail_panel.setVisible(False)
            # Set card size to XL for catalog browsing
            if hasattr(self, 'card_size_slider'):
                self._pre_catalog_card_size = self.card_size_slider.value()
                self.card_size_slider.setValue(3)
            # Clear filters for full catalog view
            self._clear_search()
        else:
            self._detail_panel.setVisible(True)
            if hasattr(self, '_pre_catalog_sizes'):
                self._search_splitter.setSizes(self._pre_catalog_sizes)
            else:
                self._search_splitter.setSizes([Z(700), Z(360)])
            # Restore card size
            if hasattr(self, '_pre_catalog_card_size') and hasattr(self, 'card_size_slider'):
                self.card_size_slider.setValue(self._pre_catalog_card_size)
            self._do_search()

    def _catalog_close_detail(self):
        """Hide detail panel in catalog mode."""
        if getattr(self, '_catalog_mode', False):
            self._detail_panel.setVisible(False)
            self._search_splitter.setSizes([self._search_splitter.width(), 0])

    def _on_tag_clicked(self, tag):
        self.inp_search.setText(tag)
        self._do_search()
        self.status_bar.showMessage(f"Filtering by tag: {tag}", 3000)

    # ── Detail Panel ────────────────────────────────────────────────────────

    def _build_detail_panel(self):
        """Right-side detail panel — asset management hub with preview, rating, notes, tags, collections."""
        panel = QFrame()
        panel.setStyleSheet(f"QFrame {{ background:{C('bg_panel')}; border-left:1px solid {C('border_subtle')}; }}")
        panel.setMinimumWidth(Z(320)); panel.setMaximumWidth(Z(440))
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(Z(14),Z(12),Z(14),Z(12)); lay.setSpacing(Z(8))

        # Close button — visible in catalog mode to dismiss panel
        self._detail_close_btn = QPushButton("Close Panel")
        self._detail_close_btn.setObjectName("neutral"); self._detail_close_btn.setFixedHeight(Z(26))
        self._detail_close_btn.setVisible(False)
        self._detail_close_btn.clicked.connect(self._catalog_close_detail)
        lay.addWidget(self._detail_close_btn)

        # ── Preview area (video player or thumbnail) ──────────────────────
        self._preview_stack = QStackedWidget()
        self._preview_stack.setFixedHeight(Z(200))

        # Page 0: static thumbnail
        self.detail_thumb = QLabel()
        self.detail_thumb.setFixedHeight(Z(200))
        self.detail_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_thumb.setStyleSheet(f"background:{C('bg_video')}; border-radius:{Z(6)}px; border:none;")
        self._preview_stack.addWidget(self.detail_thumb)  # index 0

        # Page 1: video player (if available)
        self._video_player = None
        self._video_widget = None
        self._audio_output = None
        if _HAS_VIDEO:
            self._video_widget = QVideoWidget()
            self._video_widget.setFixedHeight(Z(200))
            self._video_widget.setStyleSheet(f"background:{C('bg_video')}; border-radius:{Z(6)}px;")
            self._audio_output = QAudioOutput()
            self._audio_output.setVolume(0.5)
            self._video_player = QMediaPlayer()
            self._video_player.setAudioOutput(self._audio_output)
            self._video_player.setVideoOutput(self._video_widget)
            self._preview_stack.addWidget(self._video_widget)  # index 1
        lay.addWidget(self._preview_stack)

        # ── Video controls (play/pause/stop + scrub) ──────────────────────
        if _HAS_VIDEO:
            vctrl = QHBoxLayout(); vctrl.setSpacing(Z(4))
            self.btn_preview_play = QPushButton("Play")
            self.btn_preview_play.setObjectName("success"); self.btn_preview_play.setFixedHeight(Z(28))
            self.btn_preview_play.setFixedWidth(Z(60))
            self.btn_preview_play.clicked.connect(self._preview_toggle_play)
            vctrl.addWidget(self.btn_preview_play)
            self.btn_preview_stop = QPushButton("Stop")
            self.btn_preview_stop.setObjectName("neutral"); self.btn_preview_stop.setFixedHeight(Z(28))
            self.btn_preview_stop.setFixedWidth(Z(50))
            self.btn_preview_stop.clicked.connect(self._preview_stop)
            vctrl.addWidget(self.btn_preview_stop)
            self.preview_scrub = QSlider(Qt.Orientation.Horizontal)
            self.preview_scrub.setRange(0, 1000); self.preview_scrub.setFixedHeight(Z(20))
            self.preview_scrub.sliderMoved.connect(self._preview_seek)
            vctrl.addWidget(self.preview_scrub, 1)
            self.lbl_preview_time = QLabel("0:00")
            self.lbl_preview_time.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(10)}px; font-family:'Cascadia Code','JetBrains Mono',Consolas,monospace;")
            vctrl.addWidget(self.lbl_preview_time)
            lay.addLayout(vctrl)
            # Timer for scrub updates
            self._preview_timer = QTimer()
            self._preview_timer.timeout.connect(self._preview_update_scrub)
            self._preview_timer.start(250)

        # ── Title + favorite ──────────────────────────────────────────────
        title_row = QHBoxLayout(); title_row.setSpacing(Z(6))
        self.detail_title = QLabel("Select a clip")
        self.detail_title.setWordWrap(True)
        self.detail_title.setStyleSheet(f"color:{C('text')}; font-size:{Z(13)}px; font-weight:700;")
        title_row.addWidget(self.detail_title, 1)
        self.btn_detail_fav = QPushButton("\u2661")
        self.btn_detail_fav.setFixedSize(Z(32), Z(32))
        self.btn_detail_fav.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_detail_fav.setStyleSheet(
            f"font-size:{Z(18)}px; background:transparent; border:none; color:{C('text_disabled')};")
        self.btn_detail_fav.setToolTip("Toggle favorite")
        self.btn_detail_fav.clicked.connect(self._detail_toggle_fav)
        title_row.addWidget(self.btn_detail_fav)
        lay.addLayout(title_row)

        # ── Star rating ───────────────────────────────────────────────────
        self.detail_stars = StarRating(0, size=Z(18), interactive=True)
        self.detail_stars.rating_changed.connect(self._detail_set_rating)
        lay.addWidget(self.detail_stars)

        # ── Scrollable metadata + notes + tags area ───────────────────────
        meta_scroll = QScrollArea(); meta_scroll.setWidgetResizable(True)
        meta_scroll.setFrameShape(QFrame.Shape.NoFrame)
        meta_scroll.setStyleSheet("QScrollArea { background:transparent; }")
        meta_inner = QWidget(); meta_inner.setStyleSheet("background:transparent;")
        self._detail_meta_lay = QVBoxLayout(meta_inner)
        self._detail_meta_lay.setContentsMargins(0,0,0,0); self._detail_meta_lay.setSpacing(Z(4))
        meta_scroll.setWidget(meta_inner)
        lay.addWidget(meta_scroll, 1)

        # ── User notes ────────────────────────────────────────────────────
        notes_lbl = QLabel("Notes"); notes_lbl.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(10)}px; font-weight:600;")
        lay.addWidget(notes_lbl)
        self.detail_notes = QTextEdit()
        self.detail_notes.setMaximumHeight(Z(60))
        self.detail_notes.setPlaceholderText("Add notes about this clip...")
        self.detail_notes.setStyleSheet(
            "background:{C('bg_video')}; color:{C('text_soft')}; border:1px solid {C('bg_button')}; "
            f"border-radius:{Z(4)}px; font-size:{Z(11)}px; padding:{Z(6)}px;")
        self._notes_save_timer = QTimer(); self._notes_save_timer.setSingleShot(True)
        self._notes_save_timer.timeout.connect(self._detail_save_notes)
        self.detail_notes.textChanged.connect(lambda: self._notes_save_timer.start(800))
        lay.addWidget(self.detail_notes)

        # ── User tags ─────────────────────────────────────────────────────
        tags_lbl = QLabel("My Tags (comma-separated)")
        tags_lbl.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(10)}px; font-weight:600;")
        lay.addWidget(tags_lbl)
        self.detail_user_tags = QLineEdit()
        self.detail_user_tags.setPlaceholderText("e.g. hero-shot, b-roll, client-xyz")
        self.detail_user_tags.setFixedHeight(Z(28))
        self.detail_user_tags.setStyleSheet(
            "background:{C('bg_video')}; color:{C('accent_hover')}; border:1px solid {C('bg_button')}; "
            f"border-radius:{Z(4)}px; font-size:{Z(11)}px; padding:{Z(4)}px {Z(8)}px;")
        self._tags_save_timer = QTimer(); self._tags_save_timer.setSingleShot(True)
        self._tags_save_timer.timeout.connect(self._detail_save_user_tags)
        self.detail_user_tags.textChanged.connect(lambda: self._tags_save_timer.start(800))
        lay.addWidget(self.detail_user_tags)

        # ── Collection management ─────────────────────────────────────────
        coll_row = QHBoxLayout(); coll_row.setSpacing(Z(4))
        coll_lbl = QLabel("Collections:"); coll_lbl.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(10)}px; font-weight:600;")
        coll_row.addWidget(coll_lbl)
        self.detail_coll_combo = QComboBox(); self.detail_coll_combo.setFixedHeight(Z(26))
        self.detail_coll_combo.setMinimumWidth(Z(100))
        self.detail_coll_combo.addItem("Add to collection...")
        coll_row.addWidget(self.detail_coll_combo, 1)
        btn_add_coll = QPushButton("+"); btn_add_coll.setObjectName("success")
        btn_add_coll.setFixedSize(Z(26), Z(26)); btn_add_coll.setToolTip("Add to selected collection")
        btn_add_coll.clicked.connect(self._detail_add_to_collection)
        coll_row.addWidget(btn_add_coll)
        btn_new_coll = QPushButton("New"); btn_new_coll.setObjectName("neutral")
        btn_new_coll.setFixedSize(Z(40), Z(26)); btn_new_coll.setToolTip("Create new collection")
        btn_new_coll.clicked.connect(self._detail_create_collection)
        coll_row.addWidget(btn_new_coll)
        lay.addLayout(coll_row)

        # Collection chips (shows which collections this clip belongs to)
        self._detail_coll_chips = QWidget(); self._detail_coll_chips.setStyleSheet("background:transparent;")
        self._detail_coll_chips_lay = FlowLayout(self._detail_coll_chips, h_spacing=Z(4), v_spacing=Z(4))
        self._detail_coll_chips_lay.setContentsMargins(0,0,0,0)
        lay.addWidget(self._detail_coll_chips)

        # ── Action buttons ────────────────────────────────────────────────
        btn_row1 = QHBoxLayout()
        self.btn_detail_play = QPushButton("Open File")
        self.btn_detail_play.setObjectName("success"); self.btn_detail_play.setFixedHeight(Z(30))
        self.btn_detail_play.clicked.connect(self._detail_play)
        btn_row1.addWidget(self.btn_detail_play)
        self.btn_detail_copy_m3u8 = QPushButton("Copy M3U8")
        self.btn_detail_copy_m3u8.setObjectName("neutral"); self.btn_detail_copy_m3u8.setFixedHeight(Z(30))
        self.btn_detail_copy_m3u8.clicked.connect(self._detail_copy_m3u8)
        btn_row1.addWidget(self.btn_detail_copy_m3u8)
        self.btn_detail_open_folder = QPushButton("Folder")
        self.btn_detail_open_folder.setObjectName("neutral"); self.btn_detail_open_folder.setFixedHeight(Z(30))
        self.btn_detail_open_folder.clicked.connect(self._detail_open_file)
        btn_row1.addWidget(self.btn_detail_open_folder)
        self.btn_detail_source = QPushButton("Web")
        self.btn_detail_source.setObjectName("neutral"); self.btn_detail_source.setFixedHeight(Z(30))
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
            f"font-size:{Z(18)}px; background:transparent; border:none; "
            f"color:{C('error') if fav else C('border_light')};")

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
                tw, _th = Z(408), Z(200)
                scaled = pm.scaled(tw, _th, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
                canvas = QPixmap(tw, _th); canvas.fill(QColor(C('bg_deep')))
                painter = QPainter(canvas)
                painter.drawPixmap((tw-scaled.width())//2, (_th-scaled.height())//2, scaled)
                painter.end()
                self.detail_thumb.setPixmap(canvas)
            else:
                self.detail_thumb.setText("No thumbnail")
                self.detail_thumb.setStyleSheet(
                    f"background:{C('bg_video')}; border-radius:{Z(6)}px; color:{C('border_light')}; font-size:{Z(12)}px;")

        # ── Metadata rows ─────────────────────────────────────────────────
        while self._detail_meta_lay.count():
            item = self._detail_meta_lay.takeAt(0)
            if item.widget(): item.widget().deleteLater()

        meta_fields = [
            ('Creator',    _g('creator'),    C('warning')),
            ('Collection', _g('collection'), C('success')),
            ('Resolution', _g('resolution'), C('purple')),
            ('Duration',   _g('duration'),   C('accent')),
            ('FPS',        _g('frame_rate'), C('accent')),
            ('Camera',     _g('camera'),     C('text')),
            ('Formats',    _g('formats'),    C('text')),
            ('Status',     ('\u2713 Downloaded' if has_local else ('\u2717 Error' if _g('dl_status')=='error' else '\u2014')),
                          C('success') if has_local else (C('error') if _g('dl_status')=='error' else C('border_light'))),
            ('Clip ID',    clip_id,          C('text_muted')),
        ]
        for label, val, color in meta_fields:
            if not val: continue
            row_w = QWidget(); row_w.setStyleSheet("background:transparent;")
            row_h = QHBoxLayout(row_w); row_h.setContentsMargins(0,0,0,0); row_h.setSpacing(Z(8))
            lbl_k = QLabel(label+":"); lbl_k.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(10)}px; font-weight:600;"); lbl_k.setFixedWidth(Z(72))
            lbl_v = QLabel(val); lbl_v.setStyleSheet(f"color:{color}; font-size:{Z(10)}px;"); lbl_v.setWordWrap(True)
            row_h.addWidget(lbl_k); row_h.addWidget(lbl_v, 1)
            self._detail_meta_lay.addWidget(row_w)

        # Artlist tags (clickable)
        tags_raw = _g('tags')
        tags = [t.strip() for t in tags_raw.split(',') if t.strip()]
        if tags:
            tag_sep = QLabel("Tags"); tag_sep.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(10)}px; font-weight:600; margin-top:{Z(4)}px;")
            self._detail_meta_lay.addWidget(tag_sep)
            chips_w = QWidget(); chips_w.setStyleSheet("background:transparent;")
            chips_lay = FlowLayout(chips_w, h_spacing=Z(4), v_spacing=Z(4))
            chips_lay.setContentsMargins(0,0,0,0)
            for t in tags:
                chip = QPushButton(t); chip.setObjectName('tag-chip'); chip.setFixedHeight(Z(18))
                chip.clicked.connect(lambda _, tag=t: self._on_tag_clicked(tag))
                chips_lay.addWidget(chip)
            chips_w.setLayout(chips_lay)
            self._detail_meta_lay.addWidget(chips_w)

        if m3u8:
            url_lbl = QLabel("M3U8:"); url_lbl.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(10)}px; font-weight:600; margin-top:4px;")
            self._detail_meta_lay.addWidget(url_lbl)
            url_val = QLabel(m3u8[:60]+("..." if len(m3u8)>60 else ""))
            url_val.setStyleSheet(f"color:{C('accent_hover')}; font-size:{Z(9)}px; font-family:'Cascadia Code','JetBrains Mono',Consolas,monospace;")
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
                chip.setFixedHeight(Z(18))
                chip.setStyleSheet(
                    f"background:{c['color']}33; color:{c['color']}; "
                    f"font-size:{Z(9)}px; font-weight:700; padding:{Z(1)}px {Z(6)}px; "
                    f"border-radius:{Z(3)}px; border:1px solid {c['color']}55;")
                chip.setCursor(Qt.CursorShape.PointingHandCursor)
                chip.setToolTip(f"Remove from {c['name']}")
                cid_copy = c['id']
                clip_id_copy = clip_id
                chip.clicked.connect(lambda _, ci=cid_copy, cli=clip_id_copy: self._detail_remove_from_collection(cli, ci))
                self._detail_coll_chips_lay.addWidget(chip)
        except Exception: pass

    def _card_highlight(self, card, style="selected"):
        if style == "selected":
            card.setStyleSheet(f"QFrame#clip-card {{ border: 1px solid {C('accent')}; background-color: {C('sel_bg')}; }}")
        elif style == "multi":
            card.setStyleSheet(f"QFrame#clip-card {{ border: 1px solid {C('purple')}; background-color: {C('multi_bg')}; }}")

    def _card_unhighlight(self, card):
        try: card.setStyleSheet("")
        except RuntimeError: pass

    def _deselect_all_cards(self):
        for c, _r in getattr(self, '_selected_cards', []):
            self._card_unhighlight(c)
        self._selected_cards = []
        if self._selected_card:
            self._card_unhighlight(self._selected_card)
        self._selected_card = None
        self._last_click_idx = -1
        if hasattr(self, 'lbl_result_count'):
            self._update_selection_label()

    def _select_all_cards(self):
        if not hasattr(self, 'tabs') or self.tabs.currentIndex() != 1:
            return
        self._deselect_all_cards()
        for card in self._current_cards:
            row = getattr(card, '_row', None)
            if row:
                self._selected_cards.append((card, row))
                self._card_highlight(card, "multi")
        self._last_click_idx = len(self._current_cards) - 1 if self._current_cards else -1
        self._update_selection_label()

    def _update_selection_label(self):
        n = len(getattr(self, '_card_rows_all', []))
        shown = getattr(self, '_card_show_count', n)
        sel = len(self._selected_cards)
        suffix = f" (showing {shown})" if shown < n else ""
        sel_str = f"  |  {sel} selected" if sel > 1 else ""
        self.lbl_result_count.setText(f"{n} clip{'s' if n!=1 else ''}{suffix}{sel_str}")

    def _on_card_clicked(self, row, card=None, modifiers=None):
        """Show clip in detail panel. Supports Ctrl+click multi-select and Shift+click range."""
        if modifiers is None:
            modifiers = QApplication.keyboardModifiers()
        card_idx = self._current_cards.index(card) if card and card in self._current_cards else -1

        # Ctrl+click: toggle card in multi-select
        if modifiers & Qt.KeyboardModifier.ControlModifier and card:
            existing = [c for c, r in self._selected_cards if c is card]
            if existing:
                self._selected_cards = [(c, r) for c, r in self._selected_cards if c is not card]
                self._card_unhighlight(card)
            else:
                self._selected_cards.append((card, row))
                self._card_highlight(card, "multi")
            self._last_click_idx = card_idx
            self._update_selection_label()
            return

        # Shift+click: range select
        if modifiers & Qt.KeyboardModifier.ShiftModifier and card and self._last_click_idx >= 0:
            start = min(self._last_click_idx, card_idx)
            end = max(self._last_click_idx, card_idx)
            for c, _r in self._selected_cards:
                self._card_unhighlight(c)
            self._selected_cards = []
            for i in range(start, end + 1):
                if i < len(self._current_cards):
                    c = self._current_cards[i]
                    r = getattr(c, '_row', None)
                    if r:
                        self._selected_cards.append((c, r))
                        self._card_highlight(c, "multi")
            self._update_selection_label()
            return

        # Normal click: single select
        if self._selected_cards:
            self._deselect_all_cards()
        if self._selected_card and self._selected_card is not card:
            self._card_unhighlight(self._selected_card)
        if card:
            self._card_highlight(card, "selected")
            self._selected_card = card
        self._last_click_idx = card_idx
        if getattr(self, '_catalog_mode', False):
            self._detail_panel.setVisible(True)
            total = self._search_splitter.width()
            self._search_splitter.setSizes([total - Z(380), Z(380)])
        self._show_detail(row)
        self._update_selection_label()

    def _on_card_press(self, event, row, card):
        """Handle left click (select) and right click (context menu) on cards."""
        if event.button() == Qt.MouseButton.RightButton:
            self._card_context_menu(event.globalPosition().toPoint(), row, card)
        else:
            mods = event.modifiers() if hasattr(event, 'modifiers') else QApplication.keyboardModifiers()
            self._on_card_clicked(row, card, mods)

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

    def _card_context_menu(self, global_pos, row, card=None):
        """Right-click context menu for clip cards — supports multi-select."""
        menu = QMenu(self)
        keys = row.keys() if hasattr(row, 'keys') else []
        def _g(k): return str(row[k] if k in keys and row[k] else '')
        cid = _g('clip_id')
        if not cid: return

        # Determine if operating on multi-selection or single card
        multi = len(self._selected_cards) > 1
        if multi:
            clip_ids = [str(r.get('clip_id', '') if isinstance(r, dict) else r['clip_id']) for _c, r in self._selected_cards]
            clip_ids = [c for c in clip_ids if c]
            sel_rows = [r for _c, r in self._selected_cards]
            header = menu.addAction(f"{len(clip_ids)} clips selected")
            header.setEnabled(False)
            menu.addSeparator()
        else:
            clip_ids = [cid]
            sel_rows = [row]

        # Select All / Deselect All
        act_sel_all = menu.addAction("Select All Visible")
        act_sel_all.triggered.connect(self._select_all_cards)
        if self._selected_cards:
            act_desel = menu.addAction("Deselect All")
            act_desel.triggered.connect(self._deselect_all_cards)
        menu.addSeparator()

        # Favorite toggle
        fav = int(_g('favorited') or 0)
        fav_label = f"Toggle Favorites ({len(clip_ids)})" if multi else ("\u2665 Unfavorite" if fav else "\u2661 Favorite")
        act_fav = menu.addAction(fav_label)
        act_fav.triggered.connect(lambda: self._ctx_toggle_favorites(clip_ids))

        # Rating submenu
        rating_menu = menu.addMenu("\u2605 Set Rating")
        for stars in range(6):
            label = "\u2605" * stars + "\u2606" * (5 - stars) if stars > 0 else "Clear Rating"
            act_r = rating_menu.addAction(label)
            act_r.triggered.connect(lambda _, r=stars: self._ctx_set_rating(clip_ids, r))

        # Collection submenu
        coll_menu = menu.addMenu("Add to Collection")
        try:
            for c in self.db.get_collections():
                act_c = coll_menu.addAction(c['name'])
                cid_copy = c['id']
                act_c.triggered.connect(lambda _, ci=cid_copy: self._ctx_add_to_collection(clip_ids, ci))
        except Exception: pass
        coll_menu.addSeparator()
        act_new_coll = coll_menu.addAction("+ New Collection...")
        act_new_coll.triggered.connect(lambda: self._ctx_new_collection(clip_ids))

        menu.addSeparator()

        # Download (single or bulk)
        has_m3u8 = [r for r in sel_rows if (r.get('m3u8_url') if isinstance(r, dict) else r['m3u8_url'])]
        if has_m3u8:
            dl_label = f"Download {len(has_m3u8)} clips" if multi else "Download"
            act_dl = menu.addAction(dl_label)
            act_dl.triggered.connect(lambda: self._start_downloads(has_m3u8))

        # Copy M3U8 (single only)
        if not multi and _g('m3u8_url'):
            act_copy = menu.addAction("Copy M3U8 URL")
            act_copy.triggered.connect(lambda: (
                QApplication.clipboard().setText(_g('m3u8_url')),
                self._toast("M3U8 copied", 'success', 1500)))

        # Open in browser
        act_browser = menu.addAction("Open in Browser" + (f" ({len(clip_ids)})" if multi else ""))
        act_browser.triggered.connect(lambda: self._ctx_open_source_urls_by_ids(clip_ids))

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
            f"font-size:{Z(18)}px; background:transparent; border:none; "
            f"color:{C('error') if new_state else C('border_light')};")
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
        lay = QVBoxLayout(w); lay.setContentsMargins(Z(24),Z(20),Z(24),Z(24)); lay.setSpacing(Z(16))

        # Stats
        grp_stats = QGroupBox("Archive Statistics"); gs = QVBoxLayout(grp_stats)
        stats_cards = QHBoxLayout()
        for attr, lbl, clr in [
            ('arc_stat_clips', 'Total Clips', C('accent')),
            ('arc_stat_m3u8',  'With M3U8',  C('success')),
            ('arc_stat_dl',    'Downloaded', C('success')),
            ('arc_stat_errors','Errors',     C('error')),
            ('arc_stat_mb',    'Disk Used',  C('purple')),
        ]:
            card = QFrame(); card.setObjectName('stat-card'); card.setFixedHeight(Z(76))
            cl = QVBoxLayout(card); cl.setContentsMargins(Z(14),Z(8),Z(14),Z(8)); cl.setSpacing(Z(2))
            lv = QLabel("—"); lv.setStyleSheet(f"font-size:{Z(22)}px; font-weight:700; color:{clr}; background:transparent;")
            lv.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ll = QLabel(lbl); ll.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(11)}px; background:transparent;")
            ll.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cl.addWidget(lv); cl.addWidget(ll); setattr(self, attr, lv); stats_cards.addWidget(card)
        gs.addLayout(stats_cards)
        rfbtn = QPushButton("Refresh Stats"); rfbtn.setObjectName("neutral"); rfbtn.setFixedHeight(Z(32)); rfbtn.setFixedWidth(Z(130))
        rfbtn.clicked.connect(self._refresh_archive_stats); gs.addWidget(rfbtn)
        lay.addWidget(grp_stats)

        # Verify
        grp_verify = QGroupBox("Verify Archive  (check local files exist on disk)")
        gv = QVBoxLayout(grp_verify)
        gv.addWidget(self._sub("Scans every downloaded clip's recorded path. Flags missing files."))
        vbtn = QPushButton("Verify Archive Integrity"); vbtn.setObjectName("warning"); vbtn.setFixedHeight(Z(38)); vbtn.setMinimumWidth(Z(200))
        vbtn.clicked.connect(self._verify_archive); gv.addWidget(vbtn)
        self.lbl_verify_result = QLabel(""); self.lbl_verify_result.setWordWrap(True)
        self.lbl_verify_result.setStyleSheet(f"color:{C('warning')};"); gv.addWidget(self.lbl_verify_result)
        rbtn = QPushButton("Reset Missing to Pending"); rbtn.setObjectName("danger"); rbtn.setFixedHeight(Z(34)); rbtn.setMinimumWidth(Z(180))
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
        scan_br = QPushButton("Browse..."); scan_br.setObjectName("neutral"); scan_br.setFixedWidth(Z(90))
        scan_br.clicked.connect(self._browse_scan_dir); scan_row.addWidget(scan_br)
        gscan.addLayout(scan_row)
        scanbtn = QPushButton("Scan & Import"); scanbtn.setObjectName("success"); scanbtn.setFixedHeight(Z(38)); scanbtn.setFixedWidth(Z(160))
        scanbtn.clicked.connect(self._scan_folder); gscan.addWidget(scanbtn)
        self.lbl_scan_result = QLabel(""); self.lbl_scan_result.setWordWrap(True)
        self.lbl_scan_result.setStyleSheet(f"color:{C('success')};"); gscan.addWidget(self.lbl_scan_result)
        lay.addWidget(grp_scan)

        # Filename template
        grp_fn = QGroupBox("Filename Template"); gfn = QVBoxLayout(grp_fn)
        gfn.addWidget(self._sub("Tokens: {title}  {clip_id}  {creator}  {collection}  {resolution}"))
        fn_row = QHBoxLayout()
        self.inp_fn_template = QLineEdit("{title}")
        fn_row.addWidget(self.inp_fn_template, 1)
        fn_save = QPushButton("Save"); fn_save.setObjectName("neutral"); fn_save.setFixedWidth(Z(70))
        fn_save.clicked.connect(self._save_fn_template); fn_row.addWidget(fn_save)
        gfn.addLayout(fn_row)
        gfn.addWidget(self._sub("Example: {creator}/{title}_{clip_id}  creates subfolders per creator."))
        self.lbl_fn_preview = QLabel("")
        self.lbl_fn_preview.setStyleSheet(f"color:{C('accent_hover')}; font-size:{Z(11)}px; font-family:'Cascadia Code','JetBrains Mono',Consolas,monospace;")
        gfn.addWidget(self.lbl_fn_preview)
        self.inp_fn_template.textChanged.connect(self._update_fn_preview)
        lay.addWidget(grp_fn)

        # Retry errors
        grp_retry = QGroupBox("Retry Errors"); gr = QVBoxLayout(grp_retry)
        gr.addWidget(self._sub("Re-queue all clips that failed download."))
        retry_row = QHBoxLayout()
        self.lbl_error_count = QLabel("0 errors"); self.lbl_error_count.setStyleSheet(f"color:{C('error')}; font-weight:600;")
        retry_row.addWidget(self.lbl_error_count)
        rbtn2 = QPushButton("Retry All Errors"); rbtn2.setObjectName("warning"); rbtn2.setFixedHeight(Z(36))
        rbtn2.clicked.connect(self._retry_all_errors); retry_row.addWidget(rbtn2)
        retry_row.addStretch(); gr.addLayout(retry_row)
        lay.addWidget(grp_retry)

        # Quick access
        grp_paths = QGroupBox("Quick Access"); gp = QVBoxLayout(grp_paths)
        path_row = QHBoxLayout()
        btn_cfg_dir = QPushButton("Open Config Directory"); btn_cfg_dir.setObjectName("neutral"); btn_cfg_dir.setFixedHeight(Z(34))
        btn_cfg_dir.setToolTip(f"Opens: {get_config_dir()}")
        btn_cfg_dir.clicked.connect(lambda: self._open_path(get_config_dir()))
        path_row.addWidget(btn_cfg_dir)
        btn_db_dir = QPushButton("Open Database File"); btn_db_dir.setObjectName("neutral"); btn_db_dir.setFixedHeight(Z(34))
        btn_db_dir.setToolTip(f"Opens: {self._db_path}")
        btn_db_dir.clicked.connect(lambda: self._open_path(os.path.dirname(self._db_path)))
        path_row.addWidget(btn_db_dir)
        btn_out_dir = QPushButton("Open Output Directory"); btn_out_dir.setObjectName("neutral"); btn_out_dir.setFixedHeight(Z(34))
        btn_out_dir.clicked.connect(lambda: self._open_path(self._out_dir()))
        path_row.addWidget(btn_out_dir)
        path_row.addStretch(); gp.addLayout(path_row)
        cfg_path_lbl = QLabel(f"Config: {get_config_dir()}")
        cfg_path_lbl.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(10)}px; font-family:'Cascadia Code','JetBrains Mono',Consolas,monospace;")
        cfg_path_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        gp.addWidget(cfg_path_lbl)
        lay.addWidget(grp_paths)

        lay.addStretch()
        return scroll

    def _open_path(self, path):
        """Open a file or directory in the system file manager."""
        try:
            os.makedirs(path, exist_ok=True)
            if sys.platform == 'win32':    os.startfile(path)
            elif sys.platform == 'darwin': subprocess.Popen(['open', path])
            else:                          subprocess.Popen(['xdg-open', path])
        except Exception as e:
            self._toast(f"Could not open: {e}", 'error', 3000)

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
                self.lbl_verify_result.setStyleSheet(f"color:{C('success')};")
                self.lbl_verify_result.setText(f"All {total} downloaded files verified OK.")
            else:
                self.lbl_verify_result.setStyleSheet(f"color:{C('error')};")
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
            self.lbl_verify_result.setStyleSheet(f"color:{C('warning')};")
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
        lay.setContentsMargins(Z(20),Z(16),Z(20),Z(16)); lay.setSpacing(Z(12))

        # ── Directory row ──────────────────────────────────────────────────
        dirgrp = QGroupBox("Download Settings")
        dglay = QVBoxLayout(dirgrp)
        dlay = QHBoxLayout()
        self.inp_dl_dir = QLineEdit()
        self.inp_dl_dir.setPlaceholderText("Select folder where MP4 files will be saved...")
        dlay.addWidget(self.inp_dl_dir, 1)
        br = QPushButton("Browse..."); br.setObjectName("neutral"); br.setFixedWidth(Z(90))
        br.clicked.connect(self._browse_dl_dir); dlay.addWidget(br)
        dglay.addLayout(dlay)
        self.chk_auto_dl = QCheckBox("Auto-download -- start downloading each clip immediately as it is scraped")
        self.chk_auto_dl.setChecked(True)
        self.chk_auto_dl.setStyleSheet(f"font-weight:500; color:{C('success')};")
        dglay.addWidget(self.chk_auto_dl)

        # ── Concurrent / retry / bandwidth settings ────────────────────────
        perf_row = QHBoxLayout(); perf_row.setSpacing(Z(12))

        perf_row.addWidget(QLabel("Concurrent:"))
        self.spin_concurrent = QSpinBox(); self.spin_concurrent.setRange(1, 32)
        self.spin_concurrent.setValue(4); self.spin_concurrent.setFixedWidth(Z(60))
        self.spin_concurrent.setToolTip("Number of parallel downloads (higher = faster bulk downloads)")
        perf_row.addWidget(self.spin_concurrent)

        perf_row.addSpacing(Z(10))
        perf_row.addWidget(QLabel("Max Retries:"))
        self.spin_max_retries = QSpinBox(); self.spin_max_retries.setRange(0, 25)
        self.spin_max_retries.setValue(5); self.spin_max_retries.setFixedWidth(Z(60))
        self.spin_max_retries.setToolTip("Auto-retry failed downloads with exponential backoff")
        perf_row.addWidget(self.spin_max_retries)

        perf_row.addSpacing(Z(10))
        perf_row.addWidget(QLabel("Speed Limit:"))
        self.spin_bw_limit = QSpinBox(); self.spin_bw_limit.setRange(0, 500000)
        self.spin_bw_limit.setValue(0); self.spin_bw_limit.setSuffix(" KB/s")
        self.spin_bw_limit.setFixedWidth(Z(120))
        self.spin_bw_limit.setToolTip("Max download speed (0 = unlimited)")
        perf_row.addWidget(self.spin_bw_limit)
        perf_row.addStretch()

        dglay.addLayout(perf_row)
        lay.addWidget(dirgrp)

        # ── Queue stats banner ─────────────────────────────────────────────
        stats_row = QHBoxLayout()
        self.lbl_dl_queue  = self._hdr_lbl("Ready: 0",      "{C('accent')}")
        self.lbl_dl_done   = self._hdr_lbl("Downloaded: 0", "{C('success')}")
        self.lbl_dl_errors = self._hdr_lbl("Errors: 0",     "{C('error')}")
        for lb in (self.lbl_dl_queue, self.lbl_dl_done, self.lbl_dl_errors):
            stats_row.addWidget(lb); stats_row.addSpacing(Z(24))
        stats_row.addStretch()
        lay.addLayout(stats_row)

        # Overall progress bar
        self.dl_overall_bar = QProgressBar()
        self.dl_overall_bar.setFixedHeight(Z(8)); self.dl_overall_bar.setTextVisible(False)
        self.dl_overall_bar.setVisible(False); lay.addWidget(self.dl_overall_bar)

        # Current item progress bar
        self.dl_item_bar = QProgressBar()
        self.dl_item_bar.setFixedHeight(Z(8)); self.dl_item_bar.setRange(0,100)
        self.dl_item_bar.setFormat("%p%")
        self.dl_item_bar.setVisible(False); lay.addWidget(self.dl_item_bar)

        self.lbl_dl_current = QLabel(""); self.lbl_dl_current.setObjectName("subtext")
        lay.addWidget(self.lbl_dl_current)

        # ── Buttons row ────────────────────────────────────────────────────
        brow = QHBoxLayout()
        self.btn_dl_all = QPushButton("⬇  Download All with M3U8")
        self.btn_dl_all.setObjectName("success"); self.btn_dl_all.setFixedHeight(Z(40))
        self.btn_dl_all.clicked.connect(self._dl_all)

        self.btn_dl_new = QPushButton("⬇  Download New Only")
        self.btn_dl_new.setObjectName("success"); self.btn_dl_new.setFixedHeight(Z(40))
        self.btn_dl_new.clicked.connect(self._dl_new)

        self.btn_dl_sel = QPushButton("⬇  Download Selected")
        self.btn_dl_sel.setObjectName("neutral"); self.btn_dl_sel.setFixedHeight(Z(40))
        self.btn_dl_sel.clicked.connect(self._dl_selected)

        self.btn_dl_stop = QPushButton("⏹  Stop")
        self.btn_dl_stop.setObjectName("danger"); self.btn_dl_stop.setFixedHeight(Z(40))
        self.btn_dl_stop.setEnabled(False); self.btn_dl_stop.clicked.connect(self._dl_stop)

        opn = QPushButton("📂  Open Folder"); opn.setObjectName("neutral"); opn.setFixedHeight(Z(40))
        opn.clicked.connect(self._open_dl_folder)

        for b in (self.btn_dl_all, self.btn_dl_new, self.btn_dl_sel, self.btn_dl_stop, opn):
            brow.addWidget(b)
        lay.addLayout(brow)

        # ── Download queue table ───────────────────────────────────────────
        lbl = QLabel("Download Queue"); lbl.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(11)}px; font-weight:600;")
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
        lbl2 = QLabel("Log"); lbl2.setStyleSheet(f"color:{C('text_muted')}; font-size:{Z(11)}px; font-weight:600;")
        lay.addWidget(lbl2)
        self.dl_log = QTextEdit(); self.dl_log.setReadOnly(True)
        self.dl_log.setMaximumHeight(Z(140))
        self.dl_log.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap); lay.addWidget(self.dl_log)

        return w

    # ── Export Tab ──────────────────────────────────────────────────────────

    def _build_export_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(Z(24),Z(20),Z(24),Z(20)); lay.setSpacing(Z(14)); lay.addStretch()

        lbl = QLabel("Export Collected Data")
        lbl.setStyleSheet(f"font-size:{Z(18)}px; font-weight:700; color:{C('text')};"); lay.addWidget(lbl)
        lay.addWidget(self._sub("All exports go to your configured output directory.")); lay.addSpacing(Z(16))

        # Export all data
        grp_all = QGroupBox("Export All Clips")
        ga = QVBoxLayout(grp_all)
        for text, obj, fn in [
            ("M3U8 URLs only  (.txt)",            "success", self._export_txt),
            ("Full metadata  (.json)",             "success", self._export_json),
            ("Media player playlist  (.m3u)",     "success", self._export_m3u),
            ("Spreadsheet  (.csv -- all fields)",  "success", self._export_csv),
            ("Export all four formats at once",   None,      self._export_all),
        ]:
            btn = QPushButton(text)
            if obj: btn.setObjectName(obj)
            btn.setFixedHeight(Z(40)); btn.setFixedWidth(Z(440)); btn.clicked.connect(fn); ga.addWidget(btn)
        lay.addWidget(grp_all)

        # Export filtered / selected
        grp_filt = QGroupBox("Export Current Search Results")
        gf = QVBoxLayout(grp_filt)
        gf.addWidget(self._sub("Exports only the clips matching your current search filters."))
        for text, obj, fn in [
            ("Filtered M3U8 URLs  (.txt)",       "neutral", lambda: self._export_txt(filtered=True)),
            ("Filtered metadata  (.json)",       "neutral", lambda: self._export_json(filtered=True)),
            ("Filtered playlist  (.m3u)",        "neutral", lambda: self._export_m3u(filtered=True)),
            ("Filtered spreadsheet  (.csv)",     "neutral", lambda: self._export_csv(filtered=True)),
        ]:
            btn = QPushButton(text)
            if obj: btn.setObjectName(obj)
            btn.setFixedHeight(Z(36)); btn.setFixedWidth(Z(440)); btn.clicked.connect(fn); gf.addWidget(btn)
        lay.addWidget(grp_filt)

        lay.addSpacing(Z(14))
        self.lbl_export_status = QLabel("")
        self.lbl_export_status.setStyleSheet(f"color:{C('success')}; font-weight:600;"); lay.addWidget(self.lbl_export_status)
        lay.addStretch()
        return w

    def _get_export_rows(self, filtered=False):
        """Get rows for export — all clips or current search results."""
        if filtered and hasattr(self, '_last_rows') and self._last_rows:
            return self._last_rows
        return self.db.all_clips()

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
        # Crawl mode
        if hasattr(self, 'combo_crawl_mode'):
            cfg['crawl_mode'] = self.combo_crawl_mode.currentData() or 'full'
        # Filename template
        if hasattr(self, 'inp_fn_template'):
            cfg['fn_template'] = self.inp_fn_template.text().strip() or '{title}'
        # UI zoom level
        if hasattr(self, '_zoom_combo'):
            cfg['ui_zoom'] = self._zoom_combo.currentData() or 1.0
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
        self.lbl_browser_status.setStyleSheet(f"color:{C('warning')}; font-weight:600;")
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
            self.lbl_browser_status.setStyleSheet(f"color:{C('success')}; font-weight:600;")
            self._check_browser_status()
        else:
            self.btn_install_browser.setText("Retry Install")
            self.lbl_browser_status.setText(
                "Install failed — check the log above. Try running: python -m playwright install chromium")
            self.lbl_browser_status.setStyleSheet(f"color:{C('error')}; font-weight:500;")

    def _start_crawl(self):
        if self.worker and self.worker.isRunning(): return

        cfg = self._collect_config()
        # Merge with existing config (preserve artlist_graphql, artlist_build_id, etc.)
        existing = load_config() or {}
        existing.update(cfg)
        cfg = existing
        save_config(cfg)
        mode = cfg.get('crawl_mode', 'full')

        # Direct HTTP mode doesn't need a browser at all
        if mode == 'direct_http':
            mode_label = 'Direct HTTP'
            self._on_log(f"Starting {mode_label} — no browser needed", "INFO")
            self.worker = DirectScrapeWorker(cfg, self.db, mode='direct_http')
            self.worker.log_signal.connect(self._on_log)
            self.worker.stats_signal.connect(self._on_stats)
            self.worker.clip_signal.connect(self._on_clip_found)
            self.worker.status_signal.connect(self._on_status)
            self.worker.finished.connect(self._on_finished)
            self.worker.start()
            self.btn_start.setEnabled(False)
            self.btn_pause.setEnabled(False)  # No pause for HTTP mode
            self.btn_stop.setEnabled(True)
            self.progress_bar.setVisible(True)
            self.tabs.setCurrentIndex(1)
            return

        # API Discovery needs browser but not the full crawl loop
        if mode == 'api_discover':
            if not _chromium_is_ready():
                self._check_browser_status()
                self._on_log("Cannot start: Chromium browser not installed. Click 'Install Browser'.", "ERROR")
                return
            mode_label = 'API Discovery'
            self._on_log(f"Starting {mode_label}...", "INFO")
            self.worker = DirectScrapeWorker(cfg, self.db, mode='api_discover')
            self.worker.log_signal.connect(self._on_log)
            self.worker.stats_signal.connect(self._on_stats)
            self.worker.clip_signal.connect(self._on_clip_found)
            self.worker.status_signal.connect(self._on_status)
            self.worker.finished.connect(self._on_finished)
            self.worker.start()
            self.btn_start.setEnabled(False)
            self.btn_pause.setEnabled(False)
            self.btn_stop.setEnabled(True)
            self.progress_bar.setVisible(True)
            self.tabs.setCurrentIndex(1)
            return

        # All other modes need browser
        if not _chromium_is_ready():
            self._check_browser_status()
            self._on_log(
                "Cannot start: Chromium browser not installed. Click 'Install Browser'.", "ERROR")
            return

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
        mode_label = {
            'full': 'Full Crawl', 'catalog_sweep': 'Catalog Sweep',
            'm3u8_only': 'M3U8 Harvest',
        }.get(mode, mode)
        self._on_log(f"Starting {mode_label} with profiles: {names} (batch={cfg.get('batch_size', 50)})", "INFO")
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
            self._set_status("Running", "{C('warning')}")
        else:
            self.worker.pause(); self.btn_pause.setText("▶  Resume")
            self._set_status("Paused", "{C('purple')}")

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
            'M3U8':C('success'),'OK':C('success'),'ERROR':C('error'),
            'WARN':C('warning'),'DEBUG':'#6a6a86',
        }.get(level,C('text'))
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_view.append(
            f'<span style="color:{clr};font-family:Consolas,monospace;font-size:{Z(12)}px;">'
            f'[{ts}] {msg}</span>')
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)
        # Auto-trim every 500 appends instead of every single one
        self._log_append_count = getattr(self, '_log_append_count', 0) + 1
        if self._log_append_count >= 500:
            self._trim_log(self.log_view, 3000)
            self._log_append_count = 0

    def _on_stats(self, s):
        self.stat_clips.setText(str(s.get('clips', 0)))
        self.stat_m3u8.setText(str(s.get('m3u8', 0)))
        self.stat_pages.setText(str(s.get('processed', 0)))
        self.stat_queued.setText(str(s.get('queued', 0)))
        self.stat_errors.setText(str(s.get('failed', 0)))
        self.lbl_clips_hdr.setText(f"Clips: {s.get('clips',0)}")
        self.lbl_m3u8_hdr.setText(f"M3U8: {s.get('m3u8',0)}")

    def _on_status(self, status):
        c = {'running':C('warning'),'stopped':C('text_muted'),'challenge':C('error')}.get(status,C('text_muted'))
        l = {'running':'Running','stopped':'Idle','challenge':'Challenge'}.get(status,'Idle')
        self._set_status(l, c)

    def _set_status(self, text, color):
        self.lbl_status_hdr.setText(f"● {text}")
        self.lbl_status_hdr.setStyleSheet(
            f"font-size:{Z(12)}px; font-weight:600; background:transparent; color:{color};")

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

        # Sort override (from catalog sort dropdown)
        sort_by = ''
        if hasattr(self, 'combo_sort'):
            sort_by = self.combo_sort.currentData() or ''

        # Run DB query in background to keep GUI responsive
        def _query():
            rows = self.db.search_assets(
                query=query, filters=filters, mode=mode,
                favorites_only=favorites_only, downloaded_only=downloaded_only,
                duration_range=duration_range, collection_id=collection_id,
                min_rating=min_rating, sort_by=sort_by)
            # Convert sqlite3.Row to plain dicts for thread-safe GUI consumption
            return DB._rows_to_dicts(rows) or []

        def _on_results(rows):
            self._last_rows = rows
            self._populate_cards(rows)
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
        self._selected_cards = []
        self._last_click_idx = -1
        # Remove old "Load More" button if present
        self._safe_remove_load_more()

        self._card_rows_all = list(rows)
        self._card_show_count = 0
        initial = 400 if getattr(self, '_catalog_mode', False) else 200
        self._append_cards(initial)

    _CARD_PAGE_SIZE = 200

    def _safe_remove_load_more(self):
        """Safely remove the Load More button, handling deleted C++ objects."""
        try:
            btn = getattr(self, '_load_more_btn', None)
            if btn is not None:
                self._card_flow.removeWidget(btn)
                btn.setParent(None)
                btn.deleteLater()
        except RuntimeError:
            pass  # C++ object already deleted
        self._load_more_btn = None

    def _append_cards(self, count):
        """Append the next `count` cards from _card_rows_all."""
        # Remove old Load More button before appending
        self._safe_remove_load_more()

        size_idx = self.card_size_slider.value() if hasattr(self, 'card_size_slider') else 1
        thumb_dir = self._thumb_dir()
        start = self._card_show_count
        end = min(start + count, len(self._card_rows_all))
        new_cards = []
        for row in self._card_rows_all[start:end]:
            card = ClipCard(row, size_idx=size_idx, thumb_dir=thumb_dir)
            card.tag_clicked.connect(self._on_tag_clicked)
            card.mousePressEvent = lambda e, r=row, c=card: self._on_card_press(e, r, c)
            self._card_flow.addWidget(card)
            self._current_cards.append(card)
            new_cards.append(card)
        self._card_show_count = end

        # Add "Load More" button if there are remaining cards
        remaining = len(self._card_rows_all) - self._card_show_count
        if remaining > 0:
            btn = QPushButton(f"Load {min(remaining, self._CARD_PAGE_SIZE)} more  ({remaining} remaining)")
            btn.setObjectName("neutral")
            btn.setFixedHeight(Z(36)); btn.setFixedWidth(Z(300))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda: self._append_cards(self._CARD_PAGE_SIZE))
            self._card_flow.addWidget(btn)
            self._load_more_btn = btn
        else:
            self._load_more_btn = None

        # Force layout recalc
        self._card_container.adjustSize()
        self._update_selection_label()

        # Deferred batch thumbnail loading — prevents UI freeze on large card sets
        self._deferred_thumb_load(new_cards)

    _THUMB_BATCH_SIZE = 30

    def _deferred_thumb_load(self, cards, idx=0):
        """Load thumbnails in batches of _THUMB_BATCH_SIZE to keep UI responsive."""
        if idx >= len(cards):
            return
        batch_end = min(idx + self._THUMB_BATCH_SIZE, len(cards))
        for i in range(idx, batch_end):
            try:
                cards[i].load_deferred_thumb()
            except RuntimeError:
                pass  # widget deleted
        # Schedule next batch
        QTimer.singleShot(0, lambda: self._deferred_thumb_load(cards, batch_end))

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

    # ── Local Folder Import ────────────────────────────────────────────────

    def _import_folder(self):
        """Open folder picker and import all video files into the database."""
        # Default to the download dir if set
        start_dir = ''
        if hasattr(self, 'inp_dl_dir') and self.inp_dl_dir.text().strip():
            start_dir = self.inp_dl_dir.text().strip()
        elif hasattr(self, 'inp_output') and self.inp_output.text().strip():
            start_dir = self.inp_output.text().strip()

        folder = QFileDialog.getExistingDirectory(
            self, "Select Video Folder to Import", start_dir)
        if not folder:
            return

        if hasattr(self, '_import_worker') and self._import_worker and self._import_worker.isRunning():
            self._toast("Import already running", 'warning', 2000)
            return

        self.btn_import_folder.setEnabled(False)
        self.btn_import_folder.setText("Importing...")
        self.lbl_import_status.setText("Scanning...")

        self._import_worker = ImportWorker(
            folder, self.db, self._thumb_dir(), recursive=True)
        self._import_worker.log_signal.connect(
            lambda msg: self.lbl_import_status.setText(msg))
        self._import_worker.progress_signal.connect(
            lambda cur, total: self.lbl_import_status.setText(f"Importing {cur}/{total}..."))
        self._import_worker.finished.connect(self._on_import_done)
        self._import_worker.start()

    def _on_import_done(self, count):
        self.btn_import_folder.setEnabled(True)
        self.btn_import_folder.setText("Import Folder")
        self.lbl_import_status.setText(f"Imported {count} clips")
        if count > 0:
            self._do_search()
            self._refresh_filter_dropdowns()
            self._update_stats()
            self._toast(f"Imported {count} video clips", 'success', 4000)
        else:
            self._toast("No new videos found to import", 'info', 3000)

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
        # Reset sort to Newest in catalog mode
        if getattr(self, '_catalog_mode', False) and hasattr(self, 'combo_sort'):
            self.combo_sort.setCurrentIndex(0)
        self._do_search()

    # ── Export ──────────────────────────────────────────────────────────────

    def _out_dir(self):
        d = self.inp_output.text().strip() if hasattr(self,'inp_output') else ''
        if not d: d = os.path.join(os.path.expanduser('~'),'ArtlistScraper','output')
        os.makedirs(d, exist_ok=True); return d

    def _ts(self): return datetime.now().strftime('%Y%m%d-%H%M%S')

    def _export_txt(self, filtered=False):
        tag = "filtered " if filtered else ""
        self.lbl_export_status.setText(f"Exporting {tag}TXT...")
        rows_snapshot = self._get_export_rows(filtered)
        def _run():
            urls = [r['m3u8_url'] if hasattr(r, '__getitem__') else r.get('m3u8_url','') for r in rows_snapshot]
            urls = [u for u in urls if u]
            if not urls: return "No video URL data."
            f = os.path.join(self._out_dir(), f"video-urls-{tag.strip()}-{self._ts()}.txt" if filtered else f"video-urls-{self._ts()}.txt")
            with open(f,'w') as fh: fh.write('\n'.join(urls)+'\n')
            return f"Saved {len(urls)} URLs  ->  {f}"
        w = BackgroundWorker(_run)
        w.result_signal.connect(lambda msg: self.lbl_export_status.setText(msg))
        w.error_signal.connect(lambda e: self.lbl_export_status.setText(f"Export error: {e}"))
        self._bg_workers.append(w)
        w.finished.connect(lambda: self._bg_workers.remove(w) if w in self._bg_workers else None)
        w.start()

    def _export_json(self, filtered=False):
        tag = "filtered " if filtered else ""
        self.lbl_export_status.setText(f"Exporting {tag}JSON...")
        rows_snapshot = self._get_export_rows(filtered)
        def _run():
            if not rows_snapshot: return "No data."
            fname = f"video-metadata-filtered-{self._ts()}.json" if filtered else f"video-metadata-{self._ts()}.json"
            f = os.path.join(self._out_dir(), fname)
            with open(f,'w') as fh:
                json.dump({'exported':datetime.now().isoformat(),'total':len(rows_snapshot),
                           'clips':[dict(r) if isinstance(r, dict) else dict(zip(r.keys(), tuple(r))) for r in rows_snapshot]}, fh, indent=2)
            return f"Saved {len(rows_snapshot)} clips  ->  {f}"
        w = BackgroundWorker(_run)
        w.result_signal.connect(lambda msg: self.lbl_export_status.setText(msg))
        w.error_signal.connect(lambda e: self.lbl_export_status.setText(f"Export error: {e}"))
        self._bg_workers.append(w)
        w.finished.connect(lambda: self._bg_workers.remove(w) if w in self._bg_workers else None)
        w.start()

    def _export_m3u(self, filtered=False):
        tag = "filtered " if filtered else ""
        self.lbl_export_status.setText(f"Exporting {tag}M3U...")
        rows_snapshot = self._get_export_rows(filtered)
        def _run():
            lines = ['#EXTM3U']
            for r in rows_snapshot:
                keys = r.keys() if hasattr(r, 'keys') else r
                local_p = str(r['local_path'] if 'local_path' in keys and r['local_path'] else '')
                m3u8    = str(r['m3u8_url']   if 'm3u8_url'   in keys and r['m3u8_url']   else '')
                title   = str(r['title'] if 'title' in keys and r['title'] else r.get('clip_id', '') if isinstance(r, dict) else r['clip_id'] or 'Video Clip')
                url = local_p if (local_p and os.path.isfile(local_p)) else m3u8
                if url:
                    lines += [f"#EXTINF:-1,{title}", url]
            if len(lines) == 1: return "No video URL data."
            fname = f"video-playlist-filtered-{self._ts()}.m3u" if filtered else f"video-playlist-{self._ts()}.m3u"
            f = os.path.join(self._out_dir(), fname)
            with open(f,'w') as fh: fh.write('\n'.join(lines)+'\n')
            return f"Saved  ->  {f}"
        w = BackgroundWorker(_run)
        w.result_signal.connect(lambda msg: self.lbl_export_status.setText(msg))
        w.error_signal.connect(lambda e: self.lbl_export_status.setText(f"Export error: {e}"))
        self._bg_workers.append(w)
        w.finished.connect(lambda: self._bg_workers.remove(w) if w in self._bg_workers else None)
        w.start()

    def _export_csv(self, filtered=False):
        tag = "filtered " if filtered else ""
        self.lbl_export_status.setText(f"Exporting {tag}CSV...")
        rows_snapshot = self._get_export_rows(filtered)
        def _run():
            import csv
            if not rows_snapshot: return "No data."
            fname = f"video-metadata-filtered-{self._ts()}.csv" if filtered else f"video-metadata-{self._ts()}.csv"
            f = os.path.join(self._out_dir(), fname)
            fields = ['clip_id','title','creator','collection','tags','resolution',
                      'duration','frame_rate','camera','formats','m3u8_url','source_url','found_at']
            with open(f,'w',newline='',encoding='utf-8') as fh:
                wr = csv.DictWriter(fh, fieldnames=fields, extrasaction='ignore')
                wr.writeheader()
                for r in rows_snapshot:
                    rd = r if isinstance(r, dict) else dict(zip(r.keys(), tuple(r)))
                    wr.writerow({k: rd.get(k, '') for k in fields})
            return f"Saved {len(rows_snapshot)} rows  ->  {f}"
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
        si.setForeground(QColor(C('text_muted'))); self.dl_table.setItem(r, 1, si)
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
        clr = {'OK':C('success'),'ERROR':C('error'),'WARN':C('warning')}.get(level,C('text'))
        self.dl_log.append(
            f'<span style="color:{clr};font-family:Consolas;font-size:{Z(11)}px;">'
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
                si.setForeground(QColor(C('warning')))
            else:
                si = QTableWidgetItem("Downloading")
                si.setForeground(QColor(C('warning')))
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
                si.setForeground(QColor(C('success')))
                self.dl_table.setItem(r, 1, si)
                fi = QTableWidgetItem(os.path.basename(path_or_err))
                fi.setForeground(QColor(C('accent')))
                fi.setData(_LOCAL_PATH_ROLE, path_or_err)  # store full path
                self.dl_table.setItem(r, 3, fi)
                self.dl_table.setItem(r, 2, QTableWidgetItem("100%"))
            else:
                si = QTableWidgetItem("Error")
                si.setForeground(QColor(C('error')))
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
        # UI zoom (just sync combo — don't trigger rebuild during config load)
        if 'ui_zoom' in cfg and hasattr(self, '_zoom_combo'):
            zoom_val = cfg['ui_zoom']
            for i in range(self._zoom_combo.count()):
                if abs(self._zoom_combo.itemData(i) - zoom_val) < 0.01:
                    self._zoom_combo.blockSignals(True)
                    self._zoom_combo.setCurrentIndex(i)
                    self._zoom_combo.blockSignals(False)
                    break
        # Crawl mode
        if 'crawl_mode' in cfg and hasattr(self, 'combo_crawl_mode'):
            for i in range(self.combo_crawl_mode.count()):
                if self.combo_crawl_mode.itemData(i) == cfg['crawl_mode']:
                    self.combo_crawl_mode.setCurrentIndex(i)
                    break

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
                "Video Scraper", "Still running in background.",
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
        if hasattr(self, '_import_worker') and self._import_worker and self._import_worker.isRunning():
            self._import_worker.stop(); self._import_worker.wait(3000)
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
    # Suppress noisy Qt warnings (font point-size, multimedia info)
    os.environ.setdefault('QT_LOGGING_RULES', 'qt.multimedia.ffmpeg.warning=false;qt.qpa.fonts.warning=false')
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.RoundPreferFloor)
    app = QApplication(sys.argv)
    _init_dpi()

    # Set default app font to prevent QFont::setPointSize(-1) warnings
    _app_font = app.font()
    _app_font.setPixelSize(max(1, int(13 * _dpi_factor)))
    app.setFont(_app_font)

    # Load saved zoom and theme from config BEFORE building UI so Z() and C() are correct
    _startup_cfg = load_config()
    _startup_zoom = _startup_cfg.get('ui_zoom', 1.0)
    _startup_theme = _startup_cfg.get('theme', 'OLED')
    _ui_scale = _startup_zoom
    _set_theme(_startup_theme)

    # Scale ClipCard sizes for saved zoom
    _sf = _startup_zoom * _dpi_factor
    ClipCard.SIZES = [
        (int(160 * _sf), int(90 * _sf)),
        (int(200 * _sf), int(112 * _sf)),
        (int(240 * _sf), int(135 * _sf)),
        (int(320 * _sf), int(180 * _sf)),
    ]

    app.setStyleSheet(_build_stylesheet(_startup_zoom, _startup_theme))
    app.setApplicationName("Video Scraper")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
