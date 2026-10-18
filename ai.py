"""AI backend (NVIDIA or Anthropic). All prompts live here."""
import base64
import io

import config
from utils import extract_cards

enabled = bool(config.NVIDIA_API_KEY if config.AI_PROVIDER == "nvidia" else config.ANTHROPIC_API_KEY)
_client = None

PLAIN = "Use plain text only: no Markdown headings, bold or tables. Short hyphen lists are fine."

TUTOR_SYSTEM = (
    "You are a study assistant inside a Telegram chat. "
    f"{config.STUDENT_CONTEXT}\n"
    "Explain clearly and simply, with one concrete example. For code, use C++ or Python as fits the question. "
    "Keep replies short enough to read on a phone (under about 250 words) unless the student asks for depth. "
    "If the question rests on a wrong assumption, say so politely. "
    f"{PLAIN} Indent code by four spaces."
)

CODE_HINT_SYSTEM = (
    "You are a programming tutor. The student is learning, so do NOT give the full corrected program. "
    "Find where the problem is, explain why it happens, and give a hint or the smallest relevant snippet. "
    "If there are several issues, list them in order of importance, then suggest what to try next. "
    f"{config.STUDENT_CONTEXT} {PLAIN} Indent code by four spaces."
)

CODE_FIX_SYSTEM = (
    "You are a programming tutor. Give the corrected code, then explain in a few lines what was wrong "
    f"and what you changed. {config.STUDENT_CONTEXT} {PLAIN} Indent code by four spaces."
)

SUMMARY_SYSTEM = (
    "You summarize study material for revision. Give 5 to 8 hyphen bullets with the key points, "
    f"then one line starting 'Remember:' with the single most testable fact. {PLAIN}"
)

CARDS_SYSTEM = (
    "You write flashcards from study material. Each card tests one idea. Questions must make sense "
    "without seeing the material. Answers are one to three sentences. "
    'Reply with ONLY a JSON array like [{"q": "...", "a": "..."}] and nothing else.'
)


def _get_client():
    global _client
    if _client is None:
        if config.AI_PROVIDER == "nvidia":
            from openai import AsyncOpenAI

            _client = AsyncOpenAI(base_url=config.NVIDIA_BASE_URL, api_key=config.NVIDIA_API_KEY)
        else:
            from anthropic import AsyncAnthropic

            _client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


async def complete(system, messages, max_tokens=1200):
    client = _get_client()
    if config.AI_PROVIDER == "nvidia":
        resp = await client.chat.completions.create(
            model=config.AI_MODEL,
            messages=[{"role": "system", "content": system}] + messages,
            max_tokens=max_tokens,
            temperature=0.4,
        )
        return (resp.choices[0].message.content or "").strip() or "(no response)"
    resp = await client.messages.create(
        model=config.AI_MODEL, max_tokens=max_tokens, system=system, messages=messages
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip() or "(no response)"


def pdf_block(data: bytes):
    """Prepare a PDF for the model.

    Anthropic can read PDFs directly. NVIDIA's chat models can't, so we extract the text first.
    Raises ValueError if no text can be extracted (e.g. a scanned PDF).
    """
    if config.AI_PROVIDER != "nvidia":
        b64 = base64.standard_b64encode(data).decode()
        return [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}]
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    if not text:
        raise ValueError("no extractable text in PDF")
    return text[:30000]


def _content(source, instruction):
    if isinstance(source, list):  # Anthropic PDF blocks
        return source + [{"type": "text", "text": instruction}]
    return f"{instruction}\n\n---\n{source}"


async def chat(messages):
    return await complete(TUTOR_SYSTEM, messages)


async def code_help(text, full=False):
    system = CODE_FIX_SYSTEM if full else CODE_HINT_SYSTEM
    return await complete(system, [{"role": "user", "content": text}])


async def summarize(source):
    msg = _content(source, "Summarize this for revision.")
    return await complete(SUMMARY_SYSTEM, [{"role": "user", "content": msg}], max_tokens=900)


async def generate_cards(source, n=8):
    msg = _content(source, f"Create {n} flashcards from this material.")
    raw = await complete(CARDS_SYSTEM, [{"role": "user", "content": msg}], max_tokens=2000)
    return extract_cards(raw)[:n]
