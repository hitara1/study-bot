"""Telegram study bot: reminders, deadlines, focus timer, streaks, flashcards, notes, timetable, AI helpers."""
import html
import logging
import re
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import ai
import config
import db
from utils import (
    DAY_LABELS,
    DAY_NAMES,
    bar,
    chunk_text,
    compute_streak,
    fmt_minutes,
    parse_hhmm,
    parse_when,
    rel_time,
    trim_history,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
# httpx logs full request URLs, and Telegram's URL contains the bot token. Keep it out of logs.
for _noisy in ("httpx", "httpx2", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger("studybot")
# These loggers print full request URLs, and Telegram's URL contains the bot token. Keep it out of logs.
for _noisy in ("httpx", "httpx2", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

MAX_DOC_BYTES = 15 * 1024 * 1024
TEXT_EXT = (".txt", ".md", ".py", ".cpp", ".c", ".h", ".hpp", ".java", ".csv")

HELP = """Study bot

Focus and habits
/focus [min] [subject] - focus timer (default 25 min)
/stopfocus - end the timer early (logs time so far)
/break [min] - break timer (default 5)
/log <min> [subject] - log study time by hand
/streak - current and longest streak
/stats - this week's study time

Deadlines and reminders
/add <title> <date> [time] - e.g. /add Networking CAT fri 14:00
/deadlines - what's coming up
/done <id>, /deldeadline <id>
/remind <HH:MM> <text> - daily reminder
/reminders, /delremind <id>

Timetable
/tt add <day> <HH:MM-HH:MM> <unit> [| room]
/tt - whole week, /tt del <id>
/today - today's classes and deadlines

Notes
/note <unit> | <text>
/notes [unit], /delnote <id>

Flashcards
/card [subject |] <question> | <answer>
/quiz [subject] - spaced-repetition drill
/cards [subject], /delcard <id>

AI
/explain <concept>
/makecards <unit> - cards from your saved notes
/makecards <subject> | <pasted notes>
/practice <unit> - practice questions with hidden answers
/codehelp <code + problem> - hints only
/fixcode <code + problem> - full fix
/summarize <text> - or send a PDF/txt with /summarize as caption
Or just send any question as a normal message.

/id shows your Telegram ID."""

COMMANDS = [
    ("focus", "Start a focus timer"),
    ("stopfocus", "End the focus timer"),
    ("break", "Start a break timer"),
    ("log", "Log study time"),
    ("streak", "Show your streak"),
    ("stats", "This week's study time"),
    ("add", "Add a deadline"),
    ("deadlines", "Upcoming deadlines"),
    ("remind", "Daily reminder"),
    ("today", "Today's plan"),
    ("tt", "Timetable"),
    ("note", "Save a note"),
    ("notes", "Show notes"),
    ("card", "Add a flashcard"),
    ("quiz", "Drill flashcards"),
    ("explain", "Explain a concept"),
    ("makecards", "Cards from notes (AI)"),
    ("practice", "Practice questions (AI)"),
    ("codehelp", "Hints for your code (AI)"),
    ("summarize", "Summarize text or a file (AI)"),
    ("help", "All commands"),
]


# ---------------------------------------------------------------- helpers
def guarded(fn):
    """Block users who aren't on the allow-list (if one is set)."""

    @wraps(fn)
    async def wrapper(update, context):
        user = update.effective_user
        if config.ALLOWED_USER_IDS and (user is None or user.id not in config.ALLOWED_USER_IDS):
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"This is a private bot. Your Telegram ID is {user.id if user else '?'}."
                )
            return
        await fn(update, context)

    return wrapper


def ids(update):
    return update.effective_user.id, update.effective_chat.id


def raw_args(update):
    """Everything after the command, with the user's line breaks preserved."""
    msg = update.effective_message
    parts = (msg.text or msg.caption or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


async def reply(update, text, **kwargs):
    for part in chunk_text(text):
        await update.effective_message.reply_text(part, **kwargs)


def streak_for(user_id):
    return compute_streak(db.session_days(user_id), datetime.now(config.TZ).date())


async def safe_send(bot, chat_id, text, **kwargs):
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except Exception:
        logger.exception("Could not send message to chat %s", chat_id)


# ---------------------------------------------------------------- basics
async def cmd_id(update, context):
    await reply(update, f"Your Telegram ID: {update.effective_user.id}")


@guarded
async def cmd_help(update, context):
    await reply(update, HELP)


# ---------------------------------------------------------------- focus, log, streak, stats
@guarded
async def cmd_focus(update, context):
    uid, chat_id = ids(update)
    name = f"focus-{uid}"
    if context.job_queue.get_jobs_by_name(name):
        await reply(update, "A focus session is already running. Use /stopfocus to end it.")
        return
    args = list(context.args)
    minutes = 25
    if args and args[0].isdigit():
        minutes = max(1, min(180, int(args.pop(0))))
    subject = " ".join(args) or "general"
    context.job_queue.run_once(
        focus_done,
        minutes * 60,
        name=name,
        chat_id=chat_id,
        user_id=uid,
        data={"minutes": minutes, "subject": subject, "start": time.time()},
    )
    await reply(update, f"Focus started: {minutes} min on {subject}. I'll ping you when it's done.")


async def focus_done(context):
    job = context.job
    d = job.data
    db.log_session(job.user_id, d["subject"], d["minutes"])
    cur, _ = streak_for(job.user_id)
    await safe_send(
        context.bot,
        job.chat_id,
        f"Time's up. {d['minutes']} min of {d['subject']} logged. Streak: {cur} day(s).\nTake a break with /break.",
    )


@guarded
async def cmd_stopfocus(update, context):
    uid, _ = ids(update)
    jobs = context.job_queue.get_jobs_by_name(f"focus-{uid}")
    if not jobs:
        await reply(update, "No focus session running.")
        return
    job = jobs[0]
    job.schedule_removal()
    elapsed = int((time.time() - job.data["start"]) / 60)
    if elapsed >= 1:
        db.log_session(uid, job.data["subject"], elapsed)
        await reply(update, f"Stopped. Logged {elapsed} min of {job.data['subject']}.")
    else:
        await reply(update, "Stopped. Under a minute, so nothing logged.")


@guarded
async def cmd_break(update, context):
    uid, chat_id = ids(update)
    name = f"break-{uid}"
    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    minutes = 5
    if context.args and context.args[0].isdigit():
        minutes = max(1, min(60, int(context.args[0])))
    context.job_queue.run_once(break_done, minutes * 60, name=name, chat_id=chat_id, user_id=uid)
    await reply(update, f"Break for {minutes} min. Step away from the screen.")


async def break_done(context):
    await safe_send(context.bot, context.job.chat_id, "Break's over. Ready for another round? /focus")


@guarded
async def cmd_log(update, context):
    uid, _ = ids(update)
    args = list(context.args)
    if not args or not args[0].isdigit() or not 1 <= int(args[0]) <= 720:
        await reply(update, "Usage: /log <minutes> [subject], e.g. /log 45 C++")
        return
    minutes = int(args.pop(0))
    subject = " ".join(args) or "general"
    db.log_session(uid, subject, minutes)
    cur, _ = streak_for(uid)
    await reply(update, f"Logged {minutes} min of {subject}. Streak: {cur} day(s).")


@guarded
async def cmd_streak(update, context):
    uid, _ = ids(update)
    cur, longest = streak_for(uid)
    today_min = db.minutes_on_day(uid, datetime.now(config.TZ).date().isoformat())
    if longest == 0:
        await reply(update, "No study logged yet. Start with /focus 25 or /log 30.")
        return
    note = "" if today_min else "\nNothing logged today yet, so study today to keep it alive."
    await reply(
        update,
        f"Current streak: {cur} day(s)\nLongest: {longest} day(s)\nToday: {fmt_minutes(today_min)}{note}",
    )


@guarded
async def cmd_stats(update, context):
    uid, _ = ids(update)
    today = datetime.now(config.TZ).date()
    start = today - timedelta(days=today.weekday())
    rows = db.minutes_by_subject(uid, start.isoformat(), today.isoformat())
    if not rows:
        await reply(update, "Nothing logged this week yet. Try /focus 25 or /log 30 C++.")
        return
    prev_rows = db.minutes_by_subject(
        uid, (start - timedelta(days=7)).isoformat(), (start - timedelta(days=1)).isoformat()
    )
    prev_total = sum(r["m"] for r in prev_rows)
    total = sum(r["m"] for r in rows)
    top = rows[0]["m"]
    lines = [f"This week (since Mon {start:%d %b})"]
    lines += [f"{r['subject']}: {bar(r['m'], top)} {fmt_minutes(r['m'])}" for r in rows]
    lines.append(f"\nTotal: {fmt_minutes(total)} (last week: {fmt_minutes(prev_total)})")
    await reply(update, "\n".join(lines))


# ---------------------------------------------------------------- deadlines and reminders
@guarded
async def cmd_add(update, context):
    uid, chat_id = ids(update)
    now = datetime.now(config.TZ)
    parsed = parse_when(raw_args(update).split(), now)
    if not parsed or not parsed[0]:
        await reply(
            update,
            "Usage: /add <title> <date> [time]\nDates: today, tomorrow, fri, 2026-11-05, 05/11. "
            "Time is optional (defaults to 23:59).\nExample: /add Networking CAT fri 14:00",
        )
        return
    title, due = parsed
    if due <= now:
        await reply(update, "That time has already passed.")
        return
    left = (due - now).total_seconds()
    did = db.add_deadline(uid, chat_id, title, int(due.timestamp()), left <= 3 * 86400, left <= 86400, left <= 3 * 3600)
    await reply(update, f"Added #{did}: {title}, due {due:%a %d %b %H:%M} (in {rel_time(left)}). I'll nudge you as it gets close.")


@guarded
async def cmd_deadlines(update, context):
    uid, _ = ids(update)
    rows = db.list_deadlines(uid)
    if not rows:
        await reply(update, "No upcoming deadlines. Add one with /add.")
        return
    now = time.time()
    lines = ["Upcoming deadlines"]
    for r in rows:
        due = datetime.fromtimestamp(r["due_ts"], config.TZ)
        lines.append(f"#{r['id']} {r['title']}: {due:%a %d %b %H:%M} (in {rel_time(r['due_ts'] - now)})")
    await reply(update, "\n".join(lines))


async def _id_command(update, context, func, ok_msg, usage):
    uid, _ = ids(update)
    if not context.args or not context.args[0].isdigit():
        await reply(update, usage)
        return
    done = func(uid, int(context.args[0]))
    await reply(update, ok_msg if done else "I couldn't find that ID.")


@guarded
async def cmd_done(update, context):
    await _id_command(update, context, db.done_deadline, "Marked as done.", "Usage: /done <id>")


@guarded
async def cmd_deldeadline(update, context):
    await _id_command(update, context, db.del_deadline, "Deleted.", "Usage: /deldeadline <id>")


@guarded
async def cmd_remind(update, context):
    uid, chat_id = ids(update)
    args = list(context.args)
    hhmm = parse_hhmm(args[0]) if args else None
    text = raw_args(update).split(None, 1)[1].strip() if len(args) > 1 else ""
    if not hhmm or not text:
        await reply(update, "Usage: /remind <HH:MM> <text>, e.g. /remind 19:00 C++ revision")
        return
    now = datetime.now(config.TZ)
    # If today's time has already passed, don't fire it immediately; start tomorrow.
    last_sent = now.date().isoformat() if hhmm <= now.strftime("%H:%M") else None
    rid = db.add_reminder(uid, chat_id, hhmm, text, last_sent)
    await reply(update, f"Reminder #{rid} set: every day at {hhmm}, \"{text}\".")


@guarded
async def cmd_reminders(update, context):
    uid, _ = ids(update)
    rows = db.list_reminders(uid)
    if not rows:
        await reply(update, "No reminders. Add one with /remind 19:00 C++ revision")
        return
    await reply(update, "Daily reminders\n" + "\n".join(f"#{r['id']} {r['hhmm']} {r['text']}" for r in rows))


@guarded
async def cmd_delremind(update, context):
    await _id_command(update, context, db.del_reminder, "Reminder deleted.", "Usage: /delremind <id>")


async def tick(context):
    """Runs every minute: fires daily reminders and deadline nudges."""
    now = datetime.now(config.TZ)
    today = now.date().isoformat()
    now_min = now.hour * 60 + now.minute

    for r in db.all_reminders():
        h, m = map(int, r["hhmm"].split(":"))
        late = now_min - (h * 60 + m)
        # 30-minute catch-up window so a short restart doesn't lose a reminder.
        if 0 <= late <= 30 and r["last_sent"] != today:
            db.mark_reminder_sent(r["id"], today)
            await safe_send(context.bot, r["chat_id"], f"Reminder: {r['text']}")

    ts = time.time()
    for d in db.pending_deadlines():
        left = d["due_ts"] - ts
        n3, n1, nh = d["n3"], d["n1"], d["nh"]
        if left <= 3 * 3600 and not nh:
            flags = (1, 1, 1)
        elif left <= 86400 and not n1:
            flags = (1, 1, nh)
        elif left <= 3 * 86400 and not n3:
            flags = (1, n1, nh)
        else:
            continue
        db.set_deadline_flags(d["id"], *flags)
        due = datetime.fromtimestamp(d["due_ts"], config.TZ)
        await safe_send(
            context.bot, d["chat_id"], f"Deadline: {d['title']} is due in {rel_time(left)} ({due:%a %d %b %H:%M})."
        )


# ---------------------------------------------------------------- timetable
def _slot_line(s):
    room = f" ({s['room']})" if s["room"] else ""
    return f"  #{s['id']} {s['start_t']}-{s['end_t']} {s['unit']}{room}"


@guarded
async def cmd_tt(update, context):
    uid, _ = ids(update)
    head, _, rest = raw_args(update).partition(" ")
    head = head.lower()
    if head == "add":
        m = re.match(r"(\S+)\s+(\d{1,2}:\d{2})-(\d{1,2}:\d{2})\s+(.+)", rest.strip(), re.S)
        dow = DAY_NAMES.get(m.group(1).lower()) if m else None
        start = parse_hhmm(m.group(2)) if m else None
        end = parse_hhmm(m.group(3)) if m else None
        if dow is None or not start or not end:
            await reply(update, "Usage: /tt add <day> <HH:MM-HH:MM> <unit> [| room]\nExample: /tt add Mon 08:00-10:00 C++ | Lab 2")
            return
        unit, _, room = m.group(4).partition("|")
        sid = db.add_slot(uid, dow, start, end, unit.strip(), room.strip())
        await reply(update, f"Added #{sid}: {DAY_LABELS[dow]} {start}-{end} {unit.strip()}")
    elif head in ("del", "delete", "rm"):
        ok = rest.strip().isdigit() and db.del_slot(uid, int(rest.strip()))
        await reply(update, "Removed." if ok else "Usage: /tt del <id> (use a valid ID from /tt)")
    else:
        rows = db.slots(uid)
        if not rows:
            await reply(update, "No timetable yet. Add a slot: /tt add Mon 08:00-10:00 C++ | Lab 2")
            return
        lines = []
        for dow in range(7):
            day = [s for s in rows if s["dow"] == dow]
            if day:
                lines.append(DAY_LABELS[dow])
                lines += [_slot_line(s) for s in day]
        await reply(update, "\n".join(lines))


@guarded
async def cmd_today(update, context):
    uid, _ = ids(update)
    now = datetime.now(config.TZ)
    lines = [f"Today, {now:%A %d %b}"]
    todays = db.slots(uid, now.weekday())
    lines += [_slot_line(s) for s in todays] if todays else ["  Nothing on the timetable."]
    soon = [d for d in db.list_deadlines(uid) if d["due_ts"] - now.timestamp() <= 7 * 86400][:3]
    if soon:
        lines += ["", "Due this week"]
        for d in soon:
            due = datetime.fromtimestamp(d["due_ts"], config.TZ)
            lines.append(f"  {d['title']}: {due:%a %H:%M} (in {rel_time(d['due_ts'] - now.timestamp())})")
    cur, _ = streak_for(uid)
    mins = db.minutes_on_day(uid, now.date().isoformat())
    lines += ["", f"Studied today: {fmt_minutes(mins)}. Streak: {cur} day(s)."]
    await reply(update, "\n".join(lines))


# ---------------------------------------------------------------- notes
@guarded
async def cmd_note(update, context):
    uid, _ = ids(update)
    unit, sep, text = raw_args(update).partition("|")
    unit, text = unit.strip(), text.strip()
    if not sep or not unit or not text:
        await reply(update, "Usage: /note <unit> | <text>\nExample: /note Networking | TCP uses a three-way handshake")
        return
    nid = db.add_note(uid, unit, text)
    await reply(update, f"Saved note #{nid} under {unit}.")


@guarded
async def cmd_notes(update, context):
    uid, _ = ids(update)
    unit = raw_args(update)
    if not unit:
        units = db.note_units(uid)
        if not units:
            await reply(update, "No notes yet. Save one with /note <unit> | <text>")
            return
        await reply(update, "Your units\n" + "\n".join(f"{u['unit']}: {u['n']} note(s)" for u in units) + "\n\nOpen one with /notes <unit>")
        return
    rows = db.notes_for_unit(uid, unit)
    if not rows:
        await reply(update, f"No notes under \"{unit}\".")
        return
    await reply(update, "\n\n".join(f"#{n['id']} {n['text']}" for n in rows))


@guarded
async def cmd_delnote(update, context):
    await _id_command(update, context, db.del_note, "Note deleted.", "Usage: /delnote <id>")


# ---------------------------------------------------------------- flashcards
@guarded
async def cmd_card(update, context):
    uid, _ = ids(update)
    parts = [p.strip() for p in raw_args(update).split("|")]
    if len(parts) == 2:
        subject, q, a = "general", parts[0], parts[1]
    elif len(parts) == 3:
        subject, q, a = parts
    else:
        await reply(update, "Usage: /card [subject |] <question> | <answer>\nExample: /card Networking | What layer is IP? | Network layer (layer 3)")
        return
    if not (subject and q and a):
        await reply(update, "Subject, question and answer can't be empty.")
        return
    cid = db.add_card(uid, subject, q, a)
    await reply(update, f"Card #{cid} added to {subject}. Drill with /quiz.")


@guarded
async def cmd_cards(update, context):
    uid, _ = ids(update)
    subject = raw_args(update) or None
    rows = db.list_cards(uid, subject)
    if not rows:
        await reply(update, "No cards found. Add one with /card or generate some with /makecards.")
        return
    total, due = db.card_counts(uid, subject)
    lines = [f"{total} card(s), {due} due now"]
    lines += [f"#{r['id']} [{r['subject']}, box {r['box']}] {r['question']}" for r in rows]
    await reply(update, "\n".join(lines))


@guarded
async def cmd_delcard(update, context):
    await _id_command(update, context, db.del_card, "Card deleted.", "Usage: /delcard <id>")


async def send_next_card(bot, chat_id, user_id, subject):
    card = db.next_due_card(user_id, subject)
    if not card:
        nxt = db.next_due_ts(user_id, subject)
        extra = f" Next card is due in {rel_time(nxt - time.time())}." if nxt else ""
        await safe_send(bot, chat_id, f"All caught up.{extra}")
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Show answer", callback_data=f"qa:{card['id']}")]])
    await safe_send(bot, chat_id, f"Q: {card['question']}\n\n[{card['subject']}, box {card['box']}]", reply_markup=kb)


@guarded
async def cmd_quiz(update, context):
    uid, chat_id = ids(update)
    subject = raw_args(update) or None
    context.user_data["quiz_subject"] = subject
    total, due = db.card_counts(uid, subject)
    if total == 0:
        await reply(update, "No cards yet. Add one with /card or generate some with /makecards.")
        return
    await reply(update, f"{due} of {total} card(s) due. Missed cards come back in 10 minutes; correct ones return after 1, 3, 7, then 16 days.")
    await send_next_card(context.bot, chat_id, uid, subject)


@guarded
async def on_quiz_button(update, context):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    kind, _, rest = query.data.partition(":")
    if kind == "qa":
        card = db.get_card(uid, int(rest))
        if not card:
            await query.edit_message_text("That card no longer exists.")
            return
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("Got it", callback_data=f"qg:{card['id']}:1"),
                InlineKeyboardButton("Missed", callback_data=f"qg:{card['id']}:0"),
            ]]
        )
        await query.edit_message_text(f"Q: {card['question']}\n\nA: {card['answer']}", reply_markup=kb)
    elif kind == "qg":
        cid, _, ok = rest.partition(":")
        card = db.grade_card(uid, int(cid), ok == "1")
        if card:
            mark = "Got it" if ok == "1" else "Missed"
            await query.edit_message_text(f"[{mark}] Q: {card['question']}\n\nA: {card['answer']}")
        await send_next_card(context.bot, update.effective_chat.id, uid, context.user_data.get("quiz_subject"))


# ---------------------------------------------------------------- AI features
async def ai_gate(update):
    if not ai.enabled:
        await reply(update, "AI features are off. Set NVIDIA_API_KEY (or ANTHROPIC_API_KEY) in the bot's environment.")
        return False
    if not db.ai_use(update.effective_user.id, config.AI_DAILY_LIMIT):
        await reply(update, f"You've used today's {config.AI_DAILY_LIMIT} AI requests. The counter resets at midnight.")
        return False
    return True


def ai_error_hint(exc):
    """Turn an API error into a message that says what to fix."""
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return f"The AI provider rejected the API key (HTTP {status}). Re-check the key in .env."
    if status == 404:
        return (
            "The AI provider returned 404. Either AI_MODEL is not a model your key can use "
            "(NVIDIA IDs look like publisher/model-name) or your account lacks access. "
            "Run `python check_ai.py` on the machine running the bot to see which models work."
        )
    if status == 429:
        return "The AI provider is rate limiting you. Wait a minute and try again."
    return "The AI request failed. The bot's console has the details; `python check_ai.py` tests your settings."


async def ai_call(update, context, coro):
    """Await an AI coroutine with a typing indicator and a friendly error message."""
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        return await coro
    except Exception as exc:
        logger.exception("AI call failed")
        await reply(update, ai_error_hint(exc))
        return None


async def tutor_reply(update, context, prompt):
    if not await ai_gate(update):
        return
    history = context.user_data.setdefault("history", [])
    messages = history + [{"role": "user", "content": prompt}]
    answer = await ai_call(update, context, ai.chat(messages))
    if answer is None:
        return
    history[:] = trim_history(messages + [{"role": "assistant", "content": answer}])
    await reply(update, answer)


@guarded
async def cmd_explain(update, context):
    topic = raw_args(update)
    if not topic:
        await reply(update, "Usage: /explain <concept>, e.g. /explain what is a deadlock?")
        return
    await tutor_reply(update, context, f"Explain this for my studies: {topic}")


@guarded
async def on_chat(update, context):
    await tutor_reply(update, context, update.effective_message.text[:6000])


async def _code_help(update, context, full):
    text = raw_args(update)
    if not text:
        cmd = "/fixcode" if full else "/codehelp"
        await reply(update, f"Usage: {cmd} <paste your code and describe the problem>")
        return
    if not await ai_gate(update):
        return
    answer = await ai_call(update, context, ai.code_help(text[:12000], full))
    if answer:
        await reply(update, answer)


@guarded
async def cmd_codehelp(update, context):
    await _code_help(update, context, full=False)


@guarded
async def cmd_fixcode(update, context):
    await _code_help(update, context, full=True)


@guarded
async def cmd_summarize(update, context):
    msg = update.effective_message
    text = raw_args(update)
    if not text and msg.reply_to_message:
        text = msg.reply_to_message.text or msg.reply_to_message.caption or ""
    if not text:
        await reply(update, "Usage: /summarize <text>, or reply to a message with /summarize, or send a PDF/txt with /summarize as the caption.")
        return
    if not await ai_gate(update):
        return
    answer = await ai_call(update, context, ai.summarize(text[:30000]))
    if answer:
        await reply(update, answer)


def _resolve_source(update, args):
    """Work out (subject, text) from: 'subject | text', a replied-to message, a unit name, or pasted text."""
    uid = update.effective_user.id
    msg = update.effective_message
    subject, text = "", ""
    if "|" in args:
        subject, text = (p.strip() for p in args.split("|", 1))
    elif msg.reply_to_message and (msg.reply_to_message.text or msg.reply_to_message.caption):
        subject, text = args, msg.reply_to_message.text or msg.reply_to_message.caption
    elif len(args) > 80:
        subject, text = "general", args
    else:
        subject = args
    if not text.strip() and subject:
        notes = db.notes_for_unit(uid, subject)
        text = "\n\n".join(n["text"] for n in notes)
    if not text.strip():
        return None, None
    return subject or "general", text[:30000]


SOURCE_USAGE = (
    "Give me a unit that has saved notes (/note first), e.g. {cmd} Networking\n"
    "or paste notes after a subject: {cmd} Networking | <your notes>\n"
    "or reply to a message containing notes with {cmd}."
)


@guarded
async def cmd_makecards(update, context):
    uid, _ = ids(update)
    subject, text = _resolve_source(update, raw_args(update))
    if not text:
        await reply(update, SOURCE_USAGE.format(cmd="/makecards"))
        return
    if not await ai_gate(update):
        return
    pairs = await ai_call(update, context, ai.generate_cards(text, 8))
    if not pairs:
        return
    db.add_cards(uid, subject, pairs)
    listing = "\n".join(f"{i}. {q}" for i, (q, _) in enumerate(pairs, 1))
    await reply(update, f"Added {len(pairs)} cards to \"{subject}\":\n{listing}\n\nDrill them with /quiz {subject}. Remove any you dislike with /delcard (see ids in /cards).")


@guarded
async def cmd_practice(update, context):
    subject, text = _resolve_source(update, raw_args(update))
    if not text:
        await reply(update, SOURCE_USAGE.format(cmd="/practice"))
        return
    if not await ai_gate(update):
        return
    pairs = await ai_call(update, context, ai.generate_cards(text, 5))
    if not pairs:
        return
    body = "\n\n".join(f"{i}. {html.escape(q)}\n<tg-spoiler>{html.escape(a)}</tg-spoiler>" for i, (q, a) in enumerate(pairs, 1))
    await reply(update, f"Practice: {html.escape(subject)}\nTap a blurred answer to reveal it.\n\n{body}", parse_mode=ParseMode.HTML)


@guarded
async def on_document(update, context):
    """A PDF or text file sent with /summarize or /makecards as its caption."""
    msg = update.effective_message
    doc = msg.document
    uid, _ = ids(update)
    parts = (msg.caption or "").split(None, 1)
    cmd = parts[0].lstrip("/").split("@")[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    name = (doc.file_name or "file").lower()
    is_pdf = doc.mime_type == "application/pdf" or name.endswith(".pdf")
    is_text = (doc.mime_type or "").startswith("text/") or name.endswith(TEXT_EXT)
    if doc.file_size and doc.file_size > MAX_DOC_BYTES:
        await reply(update, "That file is over 15 MB. Send a smaller one or split it.")
        return
    if not (is_pdf or is_text):
        await reply(update, "I can read PDFs and plain text files (.txt, .md, code files).")
        return
    if not await ai_gate(update):
        return
    tg_file = await doc.get_file()
    data = bytes(await tg_file.download_as_bytearray())
    try:
        source = ai.pdf_block(data) if is_pdf else data.decode("utf-8", "ignore")[:30000]
    except Exception:
        logger.exception("Could not read document")
        await reply(update, "I couldn't read any text from that PDF. If it's a scan, send one with selectable text.")
        return
    if cmd == "makecards":
        subject = rest or Path(doc.file_name or "file").stem
        pairs = await ai_call(update, context, ai.generate_cards(source, 8))
        if pairs:
            db.add_cards(uid, subject, pairs)
            await reply(update, f"Added {len(pairs)} cards to \"{subject}\" from the file. Drill them with /quiz {subject}.")
    else:
        answer = await ai_call(update, context, ai.summarize(source))
        if answer:
            await reply(update, answer)


# ---------------------------------------------------------------- app wiring
async def on_error(update, context):
    logger.error("Unhandled error", exc_info=context.error)


async def post_init(app):
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])


HANDLERS = {
    "start": cmd_help, "help": cmd_help, "id": cmd_id,
    "focus": cmd_focus, "stopfocus": cmd_stopfocus, "break": cmd_break,
    "log": cmd_log, "streak": cmd_streak, "stats": cmd_stats,
    "add": cmd_add, "deadlines": cmd_deadlines, "done": cmd_done, "deldeadline": cmd_deldeadline,
    "remind": cmd_remind, "reminders": cmd_reminders, "delremind": cmd_delremind,
    "tt": cmd_tt, "today": cmd_today,
    "note": cmd_note, "notes": cmd_notes, "delnote": cmd_delnote,
    "card": cmd_card, "cards": cmd_cards, "delcard": cmd_delcard, "quiz": cmd_quiz,
    "explain": cmd_explain, "makecards": cmd_makecards, "practice": cmd_practice,
    "codehelp": cmd_codehelp, "fixcode": cmd_fixcode, "summarize": cmd_summarize,
}


def build_app(token=None):
    app = Application.builder().token(token or config.TELEGRAM_TOKEN).post_init(post_init).build()
    for name, fn in HANDLERS.items():
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_quiz_button, pattern=r"^q[ag]:"))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.CaptionRegex(r"^/(summarize|makecards)"), on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_chat))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(tick, interval=60, first=15)
    return app


def main():
    if not config.TELEGRAM_TOKEN:
        raise SystemExit("Set TELEGRAM_TOKEN (get one from @BotFather). See README.md.")
    db.init()
    if not ai.enabled:
        logger.warning("No AI key set (NVIDIA_API_KEY or ANTHROPIC_API_KEY): AI commands will be disabled.")
    else:
        logger.info("AI provider: %s, model: %s", config.AI_PROVIDER, config.AI_MODEL)
    if not config.ALLOWED_USER_IDS:
        logger.warning("ALLOWED_USER_IDS is empty: anyone who finds the bot can use it (AI use is capped per user).")
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
