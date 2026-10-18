"""SQLite storage. Every query is scoped by user_id so users never see each other's data."""
import sqlite3
import time
from datetime import date, datetime

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
    hhmm TEXT NOT NULL, text TEXT NOT NULL, last_sent TEXT);
CREATE TABLE IF NOT EXISTS deadlines(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
    title TEXT NOT NULL, due_ts INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0,
    n3 INTEGER NOT NULL DEFAULT 0, n1 INTEGER NOT NULL DEFAULT 0, nh INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, subject TEXT NOT NULL,
    minutes INTEGER NOT NULL, day TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cards(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, subject TEXT NOT NULL,
    question TEXT NOT NULL, answer TEXT NOT NULL, box INTEGER NOT NULL DEFAULT 1,
    due_ts INTEGER NOT NULL DEFAULT 0, created_ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS notes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, unit TEXT NOT NULL,
    text TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS timetable(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, dow INTEGER NOT NULL,
    start_t TEXT NOT NULL, end_t TEXT NOT NULL, unit TEXT NOT NULL, room TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS ai_usage(
    user_id INTEGER NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(user_id, day));
"""

# Leitner boxes: days until a card comes back after a correct answer.
BOX_DAYS = {2: 1, 3: 3, 4: 7, 5: 16}
MISSED_RETRY_SECONDS = 600

_conn = None


def init(path=None):
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = sqlite3.connect(path or config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    return _conn


def conn():
    return _conn if _conn is not None else init()


def _run(sql, args=()):
    c = conn()
    with c:
        return c.execute(sql, args)


def _all(sql, args=()):
    return conn().execute(sql, args).fetchall()


def _one(sql, args=()):
    return conn().execute(sql, args).fetchone()


def _today():
    return datetime.now(config.TZ).date().isoformat()


# ---------- reminders ----------
def add_reminder(user_id, chat_id, hhmm, text, last_sent=None):
    return _run(
        "INSERT INTO reminders(user_id, chat_id, hhmm, text, last_sent) VALUES(?,?,?,?,?)",
        (user_id, chat_id, hhmm, text, last_sent),
    ).lastrowid


def list_reminders(user_id):
    return _all("SELECT * FROM reminders WHERE user_id=? ORDER BY hhmm, id", (user_id,))


def all_reminders():
    return _all("SELECT * FROM reminders")


def del_reminder(user_id, rid):
    return _run("DELETE FROM reminders WHERE id=? AND user_id=?", (rid, user_id)).rowcount > 0


def mark_reminder_sent(rid, day):
    _run("UPDATE reminders SET last_sent=? WHERE id=?", (day, rid))


# ---------- deadlines ----------
def add_deadline(user_id, chat_id, title, due_ts, n3=0, n1=0, nh=0):
    return _run(
        "INSERT INTO deadlines(user_id, chat_id, title, due_ts, n3, n1, nh) VALUES(?,?,?,?,?,?,?)",
        (user_id, chat_id, title, due_ts, int(n3), int(n1), int(nh)),
    ).lastrowid


def list_deadlines(user_id):
    return _all(
        "SELECT * FROM deadlines WHERE user_id=? AND done=0 AND due_ts>? ORDER BY due_ts",
        (user_id, int(time.time())),
    )


def pending_deadlines():
    return _all("SELECT * FROM deadlines WHERE done=0 AND due_ts>?", (int(time.time()),))


def set_deadline_flags(did, n3, n1, nh):
    _run("UPDATE deadlines SET n3=?, n1=?, nh=? WHERE id=?", (int(n3), int(n1), int(nh), did))


def done_deadline(user_id, did):
    return _run("UPDATE deadlines SET done=1 WHERE id=? AND user_id=?", (did, user_id)).rowcount > 0


def del_deadline(user_id, did):
    return _run("DELETE FROM deadlines WHERE id=? AND user_id=?", (did, user_id)).rowcount > 0


# ---------- study sessions / streaks / stats ----------
def log_session(user_id, subject, minutes, when=None):
    when = when or datetime.now(config.TZ)
    _run(
        "INSERT INTO sessions(user_id, subject, minutes, day, ts) VALUES(?,?,?,?,?)",
        (user_id, subject, int(minutes), when.date().isoformat(), int(when.timestamp())),
    )


def session_days(user_id):
    rows = _all("SELECT DISTINCT day FROM sessions WHERE user_id=?", (user_id,))
    return {date.fromisoformat(r["day"]) for r in rows}


def minutes_on_day(user_id, day_iso):
    row = _one("SELECT COALESCE(SUM(minutes),0) AS m FROM sessions WHERE user_id=? AND day=?", (user_id, day_iso))
    return row["m"]


def minutes_by_subject(user_id, start_iso, end_iso):
    return _all(
        "SELECT MIN(subject) AS subject, SUM(minutes) AS m FROM sessions "
        "WHERE user_id=? AND day BETWEEN ? AND ? GROUP BY lower(subject) ORDER BY m DESC",
        (user_id, start_iso, end_iso),
    )


# ---------- flashcards (Leitner spaced repetition) ----------
def add_card(user_id, subject, question, answer):
    return _run(
        "INSERT INTO cards(user_id, subject, question, answer, created_ts) VALUES(?,?,?,?,?)",
        (user_id, subject, question, answer, int(time.time())),
    ).lastrowid


def add_cards(user_id, subject, pairs):
    return [add_card(user_id, subject, q, a) for q, a in pairs]


def list_cards(user_id, subject=None):
    if subject:
        return _all(
            "SELECT * FROM cards WHERE user_id=? AND subject=? COLLATE NOCASE ORDER BY id", (user_id, subject)
        )
    return _all("SELECT * FROM cards WHERE user_id=? ORDER BY subject COLLATE NOCASE, id", (user_id,))


def get_card(user_id, cid):
    return _one("SELECT * FROM cards WHERE id=? AND user_id=?", (cid, user_id))


def del_card(user_id, cid):
    return _run("DELETE FROM cards WHERE id=? AND user_id=?", (cid, user_id)).rowcount > 0


def next_due_card(user_id, subject=None):
    sql = "SELECT * FROM cards WHERE user_id=? AND due_ts<=?"
    args = [user_id, int(time.time())]
    if subject:
        sql += " AND subject=? COLLATE NOCASE"
        args.append(subject)
    return _one(sql + " ORDER BY due_ts, id LIMIT 1", args)


def card_counts(user_id, subject=None):
    """Returns (total cards, cards due now)."""
    sql = "SELECT COUNT(*) AS total, COALESCE(SUM(due_ts<=?),0) AS due FROM cards WHERE user_id=?"
    args = [int(time.time()), user_id]
    if subject:
        sql += " AND subject=? COLLATE NOCASE"
        args.append(subject)
    row = _one(sql, args)
    return row["total"], row["due"]


def next_due_ts(user_id, subject=None):
    sql = "SELECT MIN(due_ts) AS t FROM cards WHERE user_id=?"
    args = [user_id]
    if subject:
        sql += " AND subject=? COLLATE NOCASE"
        args.append(subject)
    return _one(sql, args)["t"]


def grade_card(user_id, cid, correct):
    """Move the card up a box (correct) or back to box 1 (missed). Returns the card, or None."""
    card = get_card(user_id, cid)
    if not card:
        return None
    if correct:
        box = min(card["box"] + 1, 5)
        due = int(time.time() + BOX_DAYS[box] * 86400)
    else:
        box = 1
        due = int(time.time() + MISSED_RETRY_SECONDS)
    _run("UPDATE cards SET box=?, due_ts=? WHERE id=?", (box, due, cid))
    return card


# ---------- notes ----------
def add_note(user_id, unit, text):
    return _run(
        "INSERT INTO notes(user_id, unit, text, ts) VALUES(?,?,?,?)", (user_id, unit, text, int(time.time()))
    ).lastrowid


def note_units(user_id):
    return _all(
        "SELECT MIN(unit) AS unit, COUNT(*) AS n FROM notes WHERE user_id=? GROUP BY lower(unit) ORDER BY unit",
        (user_id,),
    )


def notes_for_unit(user_id, unit):
    return _all("SELECT * FROM notes WHERE user_id=? AND unit=? COLLATE NOCASE ORDER BY id", (user_id, unit))


def del_note(user_id, nid):
    return _run("DELETE FROM notes WHERE id=? AND user_id=?", (nid, user_id)).rowcount > 0


# ---------- timetable ----------
def add_slot(user_id, dow, start_t, end_t, unit, room=""):
    return _run(
        "INSERT INTO timetable(user_id, dow, start_t, end_t, unit, room) VALUES(?,?,?,?,?,?)",
        (user_id, dow, start_t, end_t, unit, room),
    ).lastrowid


def slots(user_id, dow=None):
    if dow is None:
        return _all("SELECT * FROM timetable WHERE user_id=? ORDER BY dow, start_t", (user_id,))
    return _all("SELECT * FROM timetable WHERE user_id=? AND dow=? ORDER BY start_t", (user_id, dow))


def del_slot(user_id, sid):
    return _run("DELETE FROM timetable WHERE id=? AND user_id=?", (sid, user_id)).rowcount > 0


# ---------- AI usage cap ----------
def ai_use(user_id, limit, day=None):
    """Count one AI request against today's cap. Returns False if the cap is already reached."""
    day = day or _today()
    c = conn()
    with c:
        row = c.execute("SELECT count FROM ai_usage WHERE user_id=? AND day=?", (user_id, day)).fetchone()
        if row and row["count"] >= limit:
            return False
        c.execute(
            "INSERT INTO ai_usage(user_id, day, count) VALUES(?,?,1) "
            "ON CONFLICT(user_id, day) DO UPDATE SET count=count+1",
            (user_id, day),
        )
    return True
