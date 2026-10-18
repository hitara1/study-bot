"""Settings, read from environment variables (or a .env file)."""
import os
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional when env vars come from the host
    pass


def _id_set(raw: str) -> set:
    return {int(x.strip()) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
NVIDIA_BASE_URL = os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")

# Which AI backend to use: "nvidia" or "anthropic". If unset, whichever key is present wins
# (NVIDIA first if both are set).
AI_PROVIDER = os.environ.get("AI_PROVIDER", "").lower() or (
    "nvidia" if NVIDIA_API_KEY else "anthropic"
)

# Model name, set in .env without touching code. Check build.nvidia.com for the exact
# model IDs your key can use.
_DEFAULT_MODELS = {"nvidia": "meta/llama-3.1-70b-instruct", "anthropic": "claude-sonnet-5-5"}
AI_MODEL = os.environ.get("AI_MODEL", _DEFAULT_MODELS.get(AI_PROVIDER, ""))

TZ = ZoneInfo(os.environ.get("BOT_TZ", "Africa/Nairobi"))
DB_PATH = os.environ.get("DB_PATH", "studybot.db")

# Comma-separated Telegram user IDs allowed to use the bot. Empty = anyone can.
# Use /id in the bot to find yours.
ALLOWED_USER_IDS = _id_set(os.environ.get("ALLOWED_USER_IDS", ""))

# Per-user cap on AI requests per day, so a leaked bot link can't drain your API credit.
AI_DAILY_LIMIT = int(os.environ.get("AI_DAILY_LIMIT", "40"))

# Tells the AI who it is helping, so explanations are pitched at the right level.
STUDENT_CONTEXT = os.environ.get(
    "STUDENT_CONTEXT",
    "The student is a first-year Computer Technology student in Nairobi, Kenya. "
    "Units include OOP in C++, networking, operating systems, modular programming in Python, "
    "statistics, calculus and electronics engineering. Pitch explanations at a first-year level.",
)
