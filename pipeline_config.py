# ─────────────────────────────────────────────
#  Stock Video Pipeline — Locked Feature Config
# ─────────────────────────────────────────────

# ── API Keys (loaded from api_keys.env) ──────
import os
from dotenv import load_dotenv
load_dotenv("api_keys.env")

PEXELS_KEY      = os.getenv("PEXELS_API_KEY")
PIXABAY_KEY     = os.getenv("PIXABAY_API_KEY")
COVERR_KEY      = os.getenv("COVERR_API_KEY")
FREEPIK_KEY     = os.getenv("FREEPIK_API_KEY")
SHUTTERSTOCK_KEY= os.getenv("SHUTTERSTOCK_API_KEY")
APIVIDEO_KEY    = os.getenv("APIVIDEO_API_KEY")
UNSPLASH_KEY    = os.getenv("UNSPLASH_API_KEY")

# ── Output Format ─────────────────────────────
RESOLUTION      = (1280, 720)
FPS             = 30
MAX_OUTPUT_MB   = 100          # compress if exceeded

# ── Clip Duration ─────────────────────────────
CLIP_MIN_DUR    = 4.0          # seconds
CLIP_MAX_DUR    = 7.0          # seconds

# ── CLIP Two-Step Selection ───────────────────
CLIP_N_OFFSETS  = 5            # frames sampled per clip for best-offset selection
CLIP_MODEL      = "openai/clip-vit-base-patch32"

# ── Transitions ───────────────────────────────
CUT_TYPE_DEFAULT        = "hard"       # hard cut between all clips
CUT_TYPE_TOPIC_CHANGE   = "fade"       # quick fade at major topic changes
FADE_DURATION           = 0.12        # seconds (0.1–0.15s)
WHOOSH_AT_TOPIC_CHANGE  = True
WHOOSH_VOLUME           = 0.42
TOPIC_CHANGE_PAUSE_SEC  = 1.5         # voiceover pause threshold for topic change

# ── Pacing ────────────────────────────────────
KEN_BURNS_ENABLED       = True        # slow pan/zoom on static clips
KEN_BURNS_ZOOM          = 1.05        # zoom factor (100% → 105%)
ZOOM_PUNCH_ENABLED      = True        # quick zoom on emphasis words
ZOOM_PUNCH_SCALE        = 1.10        # zoom to 110%
ZOOM_PUNCH_DURATION     = 0.3         # seconds
BROLL_ENABLED           = True        # 1-2s reaction cutaways at pauses
BROLL_DURATION          = (1.0, 2.0)  # min/max seconds

# ── CTA Card ─────────────────────────────────
CTA_ENABLED             = True
CTA_POSITIONS           = [0.25, 0.50, 0.75]   # % of video duration
CTA_DURATION            = (1.0, 2.0)            # min/max seconds
CTA_FADE                = True
CTA_TEXT                = "Abonnieren & Keine Folge Verpassen!"
CTA_BUTTON_TEXT         = "ABONNIEREN"
CTA_STYLE = {
    "bg_color"          : (10, 10, 20),
    "overlay_color"     : (0, 0, 0, 180),        # RGBA semi-transparent
    "text_color"        : "white",
    "text_font_size"    : 52,
    "text_bold"         : False,
    "gold_line_color"   : (255, 180, 0),
    "gold_line_height"  : 4,
    "button_color"      : (255, 0, 0),
    "button_text_color" : "white",
    "button_font_size"  : 46,
    "button_bold"       : True,
    "button_radius"     : 10,
}

# ── Whoosh SFX ────────────────────────────────
WHOOSH_FREQ_START   = 120
WHOOSH_FREQ_END     = 2920
WHOOSH_DURATION     = 0.5      # seconds
