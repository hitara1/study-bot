import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import ai
import config
import db
import utils

try:
    import bot
except ImportError:  # python-telegram-bot isn't installed: handler tests are skipped
    bot = None

TZ = ZoneInfo("Africa/Nairobi")


class UtilsTests(unittest.TestCase):
    def test_hhmm(self):
        self.assertEqual(utils.parse_hhmm("7:05"), "07:05")
        self.assertIsNone(utils.parse_hhmm("25:00"))
        self.assertIsNone(utils.parse_hhmm("abc"))

    def test_parse_when_weekday(self):
        now = datetime(2026, 10, 17, 13, 52, tzinfo=TZ)  # Saturday
        title, due = utils.parse_when("Networking CAT fri 14:00".split(), now)
        self.assertEqual(title, "Networking CAT")
        self.assertEqual(due, datetime(2026, 10, 23, 14, 0, tzinfo=TZ))

    def test_parse_when_default_time_and_formats(self):
        now = datetime(2026, 10, 17, 13, 52, tzinfo=TZ)
        _, due = utils.parse_when("Report 25/10".split(), now)
        self.assertEqual(due, datetime(2026, 10, 25, 23, 59, tzinfo=TZ))
        _, due = utils.parse_when("Report 2026-11-05 09:30".split(), now)
        self.assertEqual(due, datetime(2026, 11, 5, 9, 30, tzinfo=TZ))
        _, due = utils.parse_when("Lab 05/10".split(), now)  # already passed this year
        self.assertEqual(due.year, 2027)
        _, due = utils.parse_when("Quiz tomorrow 08:00".split(), now)
        self.assertEqual(due.date(), date(2026, 10, 18))

    def test_parse_when_rejects_bad_input(self):
        now = datetime(2026, 10, 17, tzinfo=TZ)
        self.assertIsNone(utils.parse_when("just a title".split(), now))
        self.assertIsNone(utils.parse_when("Report 31/02".split(), now))
        self.assertIsNone(utils.parse_when(["14:00"], now))

    def test_streak(self):
        today = date(2026, 10, 17)
        days = {today - timedelta(days=i) for i in range(3)}
        self.assertEqual(utils.compute_streak(days, today), (3, 3))
        # Studied yesterday but not yet today: streak still alive.
        self.assertEqual(utils.compute_streak({today - timedelta(days=1), today - timedelta(days=2)}, today), (2, 2))
        # Gap of two days: streak is broken, longest remembered.
        old = {today - timedelta(days=i) for i in (3, 4, 5, 6)}
        self.assertEqual(utils.compute_streak(old, today), (0, 4))
        self.assertEqual(utils.compute_streak(set(), today), (0, 0))

    def test_extract_cards(self):
        raw = 'Here you go:\n```json\n[{"q": "What is TCP?", "a": "A reliable transport protocol."}, {"q": "", "a": "x"}]\n```'
        self.assertEqual(utils.extract_cards(raw), [("What is TCP?", "A reliable transport protocol.")])
        with self.assertRaises(ValueError):
            utils.extract_cards("no json here")

    def test_chunk_and_history(self):
        text = "\n".join(["line " + str(i) * 50 for i in range(200)])
        chunks = utils.chunk_text(text, limit=500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertEqual(" ".join(chunks).split(), text.split())
        hist = [{"role": "assistant", "content": "a"}, {"role": "user", "content": "b"}]
        self.assertEqual(utils.trim_history(hist)[0]["role"], "user")

    def test_rel_time(self):
        self.assertEqual(utils.rel_time(2 * 86400 + 3 * 3600), "2d 3h")
        self.assertEqual(utils.rel_time(5400), "1h 30m")
        self.assertEqual(utils.rel_time(600), "10m")


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db.init(self.tmp.name)

    def tearDown(self):
        db.conn().close()
        os.unlink(self.tmp.name)

    def test_leitner(self):
        cid = db.add_card(1, "Net", "Q?", "A.")
        self.assertEqual(db.next_due_card(1)["id"], cid)
        db.grade_card(1, cid, True)
        card = db.get_card(1, cid)
        self.assertEqual(card["box"], 2)
        self.assertAlmostEqual(card["due_ts"], time.time() + 86400, delta=5)
        self.assertIsNone(db.next_due_card(1))
        db.grade_card(1, cid, False)
        card = db.get_card(1, cid)
        self.assertEqual(card["box"], 1)
        self.assertAlmostEqual(card["due_ts"], time.time() + 600, delta=5)
        self.assertEqual(db.card_counts(1), (1, 0))

    def test_user_scoping(self):
        cid = db.add_card(1, "Net", "Q?", "A.")
        self.assertFalse(db.del_card(2, cid))
        self.assertIsNone(db.get_card(2, cid))
        self.assertTrue(db.del_card(1, cid))

    def test_ai_limit(self):
        self.assertTrue(db.ai_use(1, 2))
        self.assertTrue(db.ai_use(1, 2))
        self.assertFalse(db.ai_use(1, 2))
        self.assertTrue(db.ai_use(2, 2))

    def test_sessions_and_stats(self):
        now = datetime.now(TZ)
        db.log_session(1, "C++", 30, now)
        db.log_session(1, "c++", 20, now)
        db.log_session(1, "Stats", 10, now)
        rows = db.minutes_by_subject(1, now.date().isoformat(), now.date().isoformat())
        self.assertEqual([(r["subject"].lower(), r["m"]) for r in rows], [("c++", 50), ("stats", 10)])
        self.assertEqual(db.session_days(1), {now.date()})

    def test_notes_case_insensitive(self):
        db.add_note(1, "Networking", "TCP handshake")
        self.assertEqual(len(db.notes_for_unit(1, "networking")), 1)
        self.assertEqual(db.note_units(1)[0]["n"], 1)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_nvidia_request_shape(self):
        create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=" hi "))]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(config, "AI_PROVIDER", "nvidia"), patch.object(config, "AI_MODEL", "m/x"), \
                patch.object(ai, "_get_client", return_value=client):
            out = await ai.complete("SYS", [{"role": "user", "content": "q"}])
        self.assertEqual(out, "hi")
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["model"], "m/x")
        self.assertEqual(kwargs["messages"][0], {"role": "system", "content": "SYS"})
        self.assertEqual(kwargs["messages"][1]["content"], "q")

    async def test_anthropic_request_shape(self):
        create = AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(type="text", text="yo")]))
        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        with patch.object(config, "AI_PROVIDER", "anthropic"), patch.object(ai, "_get_client", return_value=client):
            out = await ai.complete("SYS", [{"role": "user", "content": "q"}])
        self.assertEqual(out, "yo")
        self.assertEqual(create.call_args.kwargs["system"], "SYS")

    def test_pdf_block_per_provider(self):
        with patch.object(config, "AI_PROVIDER", "anthropic"):
            block = ai.pdf_block(b"%PDF-fake")
        self.assertEqual(block[0]["type"], "document")

    def test_closest_models(self):
        import check_ai

        ids = ["nvidia/nemotron-3-super-120b-a12b", "meta/llama-3.1-70b-instruct", "mistralai/mixtral-8x7b-instruct-v0.1"]
        self.assertEqual(check_ai.closest_models("nemotron-3.5-super-vl-120b-a12b", ids)[0], ids[0])

    def test_provider_defaults(self):
        self.assertIn(config.AI_PROVIDER, ("nvidia", "anthropic"))


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_once(self, callback, when, name=None, chat_id=None, user_id=None, data=None):
        job = SimpleNamespace(
            callback=callback, when=when, name=name, chat_id=chat_id, user_id=user_id, data=data,
            schedule_removal=lambda: self.jobs.remove(job),
        )
        self.jobs.append(job)

    def get_jobs_by_name(self, name):
        return [j for j in self.jobs if j.name == name]


def make_update(text, uid=1, chat=10):
    msg = SimpleNamespace(text=text, caption=None, reply_to_message=None, reply_text=AsyncMock(), document=None)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=uid),
        effective_chat=SimpleNamespace(id=chat),
        effective_message=msg,
        callback_query=None,
    )
    return update, msg


def make_ctx(text, user_data=None, jq=None):
    return SimpleNamespace(
        args=text.split()[1:],
        user_data=user_data if user_data is not None else {},
        bot=SimpleNamespace(send_message=AsyncMock(), send_chat_action=AsyncMock()),
        job_queue=jq or FakeJobQueue(),
    )


def sent(msg):
    return "\n".join(c.args[0] for c in msg.reply_text.call_args_list)


@unittest.skipIf(bot is None, "python-telegram-bot is not installed")
class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db.init(self.tmp.name)
        patcher = patch.object(config, "ALLOWED_USER_IDS", set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        db.conn().close()
        os.unlink(self.tmp.name)

    async def run_cmd(self, handler, text, **ctx_kwargs):
        update, msg = make_update(text)
        ctx = make_ctx(text, **ctx_kwargs)
        await handler(update, ctx)
        return msg, ctx

    async def test_deadline_flow_and_nudges(self):
        msg, _ = await self.run_cmd(bot.cmd_add, "/add Networking CAT in two hours tomorrow 08:00")
        self.assertIn("Added #1", sent(msg))
        msg, _ = await self.run_cmd(bot.cmd_deadlines, "/deadlines")
        self.assertIn("Networking CAT in two hours", sent(msg))
        # A deadline 2 hours away with no nudges sent yet gets exactly one nudge.
        db.add_deadline(1, 10, "Lab report", int(time.time() + 7200))
        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        await bot.tick(ctx)
        texts = [c.args[1] for c in ctx.bot.send_message.call_args_list]
        self.assertTrue(any("Lab report" in t for t in texts))
        ctx.bot.send_message.reset_mock()
        await bot.tick(ctx)
        self.assertFalse(any("Lab report" in c.args[1] for c in ctx.bot.send_message.call_args_list))

    async def test_add_rejects_past_and_garbage(self):
        msg, _ = await self.run_cmd(bot.cmd_add, "/add Something sometime")
        self.assertIn("Usage", sent(msg))
        msg, _ = await self.run_cmd(bot.cmd_add, "/add Old thing 2020-01-01")
        self.assertIn("passed", sent(msg))

    async def test_reminder_fires_once(self):
        now = datetime.now(config.TZ)
        db.add_reminder(1, 10, now.strftime("%H:%M"), "Revise C++")
        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        await bot.tick(ctx)
        await bot.tick(ctx)
        self.assertEqual(ctx.bot.send_message.call_count, 1)
        self.assertIn("Revise C++", ctx.bot.send_message.call_args.args[1])

    async def test_remind_command_for_past_time_waits_until_tomorrow(self):
        now = datetime.now(config.TZ)
        if now.hour == 0 and now.minute == 0:
            self.skipTest("midnight edge")
        await self.run_cmd(bot.cmd_remind, "/remind 00:00 Sleep")
        rows = db.list_reminders(1)
        self.assertEqual(rows[0]["last_sent"], now.date().isoformat())

    async def test_flashcard_quiz_flow(self):
        msg, _ = await self.run_cmd(bot.cmd_card, "/card Networking | What layer is IP? | Layer 3")
        self.assertIn("Card #1", sent(msg))
        msg, ctx = await self.run_cmd(bot.cmd_quiz, "/quiz Networking")
        self.assertIn("1 of 1", sent(msg))
        question = ctx.bot.send_message.call_args
        self.assertIn("What layer is IP?", question.args[1])

        query = SimpleNamespace(data="qa:1", answer=AsyncMock(), edit_message_text=AsyncMock())
        update, _ = make_update("")
        update.callback_query = query
        await bot.on_quiz_button(update, ctx)
        self.assertIn("Layer 3", query.edit_message_text.call_args.args[0])

        query2 = SimpleNamespace(data="qg:1:1", answer=AsyncMock(), edit_message_text=AsyncMock())
        update.callback_query = query2
        await bot.on_quiz_button(update, ctx)
        self.assertEqual(db.get_card(1, 1)["box"], 2)
        self.assertIn("All caught up", ctx.bot.send_message.call_args.args[1])

    async def test_focus_and_streak(self):
        msg, ctx = await self.run_cmd(bot.cmd_focus, "/focus 25 C++")
        self.assertIn("25 min on C++", sent(msg))
        self.assertEqual(len(ctx.job_queue.jobs), 1)
        # Second start is refused while one is running.
        update, msg2 = make_update("/focus")
        await bot.cmd_focus(update, ctx)
        self.assertIn("already running", sent(msg2))
        job = ctx.job_queue.jobs[0]
        await bot.focus_done(SimpleNamespace(job=job, bot=ctx.bot))
        self.assertEqual(db.minutes_on_day(1, datetime.now(config.TZ).date().isoformat()), 25)
        msg, _ = await self.run_cmd(bot.cmd_streak, "/streak")
        self.assertIn("Current streak: 1", sent(msg))

    async def test_timetable_and_today(self):
        msg, _ = await self.run_cmd(bot.cmd_tt, "/tt add Mon 08:00-10:00 C++ | Lab 2")
        self.assertIn("Added #1", sent(msg))
        msg, _ = await self.run_cmd(bot.cmd_tt, "/tt")
        self.assertIn("08:00-10:00 C++ (Lab 2)", sent(msg))
        msg, _ = await self.run_cmd(bot.cmd_tt, "/tt add Funday 08:00-10:00 Nothing")
        self.assertIn("Usage", sent(msg))
        msg, _ = await self.run_cmd(bot.cmd_today, "/today")
        self.assertIn("Today,", sent(msg))

    async def test_notes_and_ai_cards(self):
        await self.run_cmd(bot.cmd_note, "/note Networking | TCP uses a three-way handshake")
        reply_json = '[{"q": "What does TCP use to open a connection?", "a": "A three-way handshake."}]'
        with patch.object(ai, "enabled", True), patch.object(ai, "complete", AsyncMock(return_value=reply_json)) as fake:
            msg, _ = await self.run_cmd(bot.cmd_makecards, "/makecards Networking")
            self.assertIn("Added 1 cards", sent(msg))
            self.assertIn("three-way handshake", fake.call_args.args[1][0]["content"])
        self.assertEqual(len(db.list_cards(1, "Networking")), 1)

    async def test_practice_uses_spoilers(self):
        reply_json = '[{"q": "Q <1>?", "a": "A & B"}]'
        with patch.object(ai, "enabled", True), patch.object(ai, "complete", AsyncMock(return_value=reply_json)):
            msg, _ = await self.run_cmd(bot.cmd_practice, "/practice Stats | mean is the average of values " + "x" * 80)
        self.assertIn("<tg-spoiler>A &amp; B</tg-spoiler>", sent(msg))

    async def test_chat_keeps_history(self):
        with patch.object(ai, "enabled", True), patch.object(ai, "complete", AsyncMock(return_value="A deadlock is...")):
            update, msg = make_update("what is a deadlock?")
            ctx = make_ctx("what is a deadlock?")
            await bot.on_chat(update, ctx)
            self.assertIn("A deadlock is", sent(msg))
            self.assertEqual(len(ctx.user_data["history"]), 2)

    async def test_ai_failure_is_friendly_and_cap_applies(self):
        with patch.object(ai, "enabled", True), patch.object(ai, "complete", AsyncMock(side_effect=RuntimeError("boom"))):
            msg, _ = await self.run_cmd(bot.cmd_explain, "/explain deadlock")
            self.assertIn("AI request failed", sent(msg))
        with patch.object(ai, "enabled", True), patch.object(config, "AI_DAILY_LIMIT", 0):
            msg, _ = await self.run_cmd(bot.cmd_explain, "/explain deadlock")
            self.assertIn("AI requests", sent(msg))

    async def test_ai_404_hint_mentions_model_and_check_script(self):
        class NotFound(Exception):
            status_code = 404

        with patch.object(ai, "enabled", True), patch.object(ai, "complete", AsyncMock(side_effect=NotFound("x"))):
            msg, _ = await self.run_cmd(bot.cmd_explain, "/explain deadlock")
        self.assertIn("AI_MODEL", sent(msg))
        self.assertIn("check_ai.py", sent(msg))

    async def test_ai_404_points_to_check_script(self):
        err = RuntimeError("404 page not found")
        err.status_code = 404
        with patch.object(ai, "enabled", True), patch.object(ai, "complete", AsyncMock(side_effect=err)):
            msg, _ = await self.run_cmd(bot.cmd_explain, "/explain deadlock")
        self.assertIn("404", sent(msg))
        self.assertIn("check_ai.py", sent(msg))

    async def test_ai_disabled_message(self):
        with patch.object(ai, "enabled", False):
            msg, _ = await self.run_cmd(bot.cmd_explain, "/explain deadlock")
        self.assertIn("ANTHROPIC_API_KEY", sent(msg))

    async def test_allowlist_blocks_strangers(self):
        with patch.object(config, "ALLOWED_USER_IDS", {999}):
            msg, _ = await self.run_cmd(bot.cmd_help, "/help")
        self.assertIn("private bot", sent(msg))

    def test_app_builds_with_all_handlers(self):
        app = bot.build_app("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
        self.assertIsNotNone(app.job_queue)
        names = {h.commands for h in app.handlers[0] if hasattr(h, "commands")}
        self.assertTrue(any("quiz" in n for n in names))


if __name__ == "__main__":
    unittest.main()
