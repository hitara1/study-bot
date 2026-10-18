"""Small pure helpers: no Telegram, DB or network imports, so they are easy to test."""
import json
import re
from datetime import date, datetime, timedelta

DAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def parse_hhmm(s):
    """'7:05' -> '07:05'; returns None if it isn't a valid 24h time."""
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", s.strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def parse_date_token(tok, today):
    """Understands today, tomorrow, weekday names, YYYY-MM-DD, DD/MM and DD/MM/YYYY."""
    t = tok.strip().lower()
    if t == "today":
        return today
    if t in ("tomorrow", "tmr", "tmrw"):
        return today + timedelta(days=1)
    if t in DAY_NAMES:
        delta = (DAY_NAMES[t] - today.weekday()) % 7 or 7  # a bare weekday means the next one
        return today + timedelta(days=delta)
    try:
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?", t)
        if m:
            day, month = int(m[1]), int(m[2])
            if m[3]:
                year = int(m[3])
                return date(year + 2000 if year < 100 else year, month, day)
            d = date(today.year, month, day)
            return d if d >= today else date(today.year + 1, month, day)
    except ValueError:
        return None
    return None


def parse_when(tokens, now, default_time="23:59"):
    """['Networking', 'CAT', 'fri', '14:00'] -> ('Networking CAT', datetime) or None."""
    tokens = list(tokens)
    hhmm = None
    if tokens and parse_hhmm(tokens[-1]):
        hhmm = parse_hhmm(tokens.pop())
    if not tokens:
        return None
    d = parse_date_token(tokens[-1], now.date())
    if d is None:
        return None
    tokens.pop()
    h, m = map(int, (hhmm or default_time).split(":"))
    return " ".join(tokens), datetime(d.year, d.month, d.day, h, m, tzinfo=now.tzinfo)


def rel_time(seconds):
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def fmt_minutes(minutes):
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def bar(value, maximum, width=12):
    if not maximum or value <= 0:
        return ""
    return "█" * max(1, round(width * value / maximum))


def compute_streak(days, today):
    """days: set of dates with study activity. Returns (current_streak, longest_streak).

    The current streak stays alive if you studied yesterday but haven't yet today.
    """
    if not days:
        return 0, 0
    cur = 0
    d = today if today in days else today - timedelta(days=1)
    while d in days:
        cur += 1
        d -= timedelta(days=1)
    longest = run = 0
    prev = None
    for d in sorted(days):
        run = run + 1 if prev is not None and (d - prev).days == 1 else 1
        longest = max(longest, run)
        prev = d
    return cur, longest


def chunk_text(text, limit=3900):
    """Split long text into Telegram-sized messages, preferring line breaks."""
    text = text.strip() or "(empty)"
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


def extract_cards(raw):
    """Pull [{"q": ..., "a": ...}, ...] out of a model reply, tolerating code fences."""
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("no JSON array in reply")
    data = json.loads(raw[start : end + 1])
    cards = []
    for item in data:
        if isinstance(item, dict):
            q, a = str(item.get("q", "")).strip(), str(item.get("a", "")).strip()
            if q and a:
                cards.append((q, a))
    if not cards:
        raise ValueError("no usable cards in reply")
    return cards


def trim_history(messages, keep=8):
    """Keep the last few chat turns, making sure the list starts with a user message."""
    msgs = messages[-keep:]
    while msgs and msgs[0]["role"] != "user":
        msgs = msgs[1:]
    return msgs
