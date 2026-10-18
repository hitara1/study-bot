# Telegram Study Bot

A Telegram bot for studying: focus timer, streaks, deadlines with nudges, daily reminders, timetable, notes, spaced-repetition flashcards, and AI helpers (explain, generate cards from notes, practice questions, code hints, summaries of text and PDFs).

## Setup

1. Create the bot: message **@BotFather** on Telegram, send `/newbot`, copy the token.
2. Install and configure:
   ```
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env      # then fill in TELEGRAM_TOKEN and NVIDIA_API_KEY (or ANTHROPIC_API_KEY)
   python bot.py
   ```
3. Message your bot `/id`, put that number in `ALLOWED_USER_IDS` in `.env`, and restart. This keeps strangers from using your bot and your API credit.

## Choosing the AI provider

The bot works with either an **NVIDIA** key (build.nvidia.com, OpenAI-compatible endpoint) or an **Anthropic** key. Set one in `.env` and it is picked automatically; `AI_PROVIDER` forces a choice. Set `AI_MODEL` to the exact model ID from your provider's catalogue (the NVIDIA default is `meta/llama-3.1-70b-instruct`).

**If AI replies fail** (for example "404"), run `python check_ai.py`. It lists the model IDs your key can see, tells you if `AI_MODEL` isn't one of them, and sends a test message. NVIDIA model IDs have the form `publisher/model-name` (e.g. `meta/llama-3.1-70b-instruct`). A 404 on chat requests while the model list works usually means the model name is wrong or your NVIDIA account isn't enabled for the public API endpoints.

Differences: with NVIDIA, PDFs are converted to text first, so scanned PDFs won't work. Smaller open models also follow the "reply with JSON only" instruction less reliably, so `/makecards` and `/practice` can occasionally fail; just run them again.

## Deploy on Railway

- Push this folder to a GitHub repo and create a Railway project from it (the `Procfile` runs `python bot.py` as a worker).
- Add the variables from `.env.example` in the Railway dashboard.
- Add a **volume** mounted at `/data` and set `DB_PATH=/data/studybot.db`. Without a volume the SQLite file is wiped on every redeploy.
- Run only one instance. Two bots polling with the same token will fight each other.

## Commands

| Area | Commands |
|---|---|
| Focus | `/focus [min] [subject]`, `/stopfocus`, `/break [min]`, `/log <min> [subject]` |
| Habits | `/streak`, `/stats` |
| Deadlines | `/add <title> <date> [time]`, `/deadlines`, `/done <id>`, `/deldeadline <id>` |
| Reminders | `/remind <HH:MM> <text>`, `/reminders`, `/delremind <id>` |
| Timetable | `/tt add <day> <HH:MM-HH:MM> <unit> [\| room]`, `/tt`, `/tt del <id>`, `/today` |
| Notes | `/note <unit> \| <text>`, `/notes [unit]`, `/delnote <id>` |
| Flashcards | `/card [subject \|] <q> \| <a>`, `/quiz [subject]`, `/cards [subject]`, `/delcard <id>` |
| AI | `/explain`, `/makecards`, `/practice`, `/codehelp`, `/fixcode`, `/summarize`, or just send a question |

Examples:

```
/add Networking CAT fri 14:00
/remind 19:00 C++ revision
/tt add Mon 08:00-10:00 C++ | Lab 2
/note Networking | TCP uses a three-way handshake: SYN, SYN-ACK, ACK
/makecards Networking
/quiz Networking
/codehelp why does this segfault? <paste code>
```

Send a PDF or `.txt` file with the caption `/summarize` or `/makecards Subject` to work from a file.

## How it works

- **Deadlines** nudge you 3 days, 1 day and 3 hours before. **Reminders** are daily at the time you set (Nairobi time by default, change with `BOT_TZ`). Both are stored in SQLite and survive restarts.
- **Flashcards** use Leitner boxes: a correct answer returns after 1, 3, 7, then 16 days; a miss comes back in 10 minutes.
- **Streak** counts consecutive days with any logged study time. It stays alive until the end of the day after your last session.
- **AI chat** keeps the last few turns, so follow-ups like "give another example" work. `/codehelp` gives hints only; `/fixcode` gives the full fix.
- **Limits**: each user gets `AI_DAILY_LIMIT` AI requests per day (default 40).
- Focus and break timers live in memory, so a restart cancels any running timer.

## Files

`bot.py` handlers and scheduling · `ai.py` prompts and API calls · `db.py` SQLite · `utils.py` parsing and helpers · `config.py` settings · `test_studybot.py` tests (`python -m unittest`)
