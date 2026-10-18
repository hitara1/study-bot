"""Diagnose your AI settings.  Run:  python check_ai.py [filter-word]

Lists the model IDs your key can see (NVIDIA), then sends one tiny test request with AI_MODEL.
Never prints your API key.
"""
import asyncio
import difflib
import json
import sys
import urllib.error
import urllib.request

import config


def closest_models(model, ids, n=5):
    return difflib.get_close_matches(model, ids, n=n, cutoff=0.4)


def _http(url, key, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:  # network problems, DNS, etc.
        return None, str(e)


def check_nvidia(word=""):
    base = config.NVIDIA_BASE_URL.rstrip("/")
    key = config.NVIDIA_API_KEY
    print(f"Provider: nvidia\nBase URL: {base}\nAI_MODEL: {config.AI_MODEL}\n")
    if not key:
        print("NVIDIA_API_KEY is empty. Put it in .env.")
        return

    status, body = _http(f"{base}/models", key)
    ids = []
    if status == 200:
        ids = sorted(m["id"] for m in json.loads(body).get("data", []))
        print(f"Step 1: key accepted, {len(ids)} models listed.")
        if config.AI_MODEL in ids:
            print(f"  AI_MODEL '{config.AI_MODEL}' is in the list.")
        else:
            print(f"  AI_MODEL '{config.AI_MODEL}' is NOT in the list.")
            close = closest_models(config.AI_MODEL, ids)
            if close:
                print("  Closest matches (copy one into AI_MODEL in .env):")
                print("\n".join(f"    {c}" for c in close))
        shown = [i for i in ids if word.lower() in i.lower()] if word else ids[:40]
        label = f"matching '{word}'" if word else "first 40 (add a word to filter, e.g. python check_ai.py nemotron)"
        print(f"\nModels {label}:")
        print("\n".join(f"  {i}" for i in shown) or "  (none)")
    else:
        print(f"Step 1: listing models failed (HTTP {status}): {body[:200]}")
        if status in (401, 403):
            print("  The key was rejected. Re-copy it from build.nvidia.com into NVIDIA_API_KEY.")

    print("\nStep 2: sending a test message with AI_MODEL ...")
    status, body = _http(
        f"{base}/chat/completions",
        key,
        {"model": config.AI_MODEL, "messages": [{"role": "user", "content": "Reply with the single word: ok"}], "max_tokens": 20},
    )
    if status == 200:
        try:
            print("  Success. Model replied:", json.loads(body)["choices"][0]["message"]["content"].strip()[:80])
        except Exception:
            print("  HTTP 200 but unexpected body:", body[:200])
        return
    print(f"  Failed (HTTP {status}): {body[:300]}")
    if status == 404:
        if ids and config.AI_MODEL not in ids:
            print("  -> The model name isn't one your key lists. Use an ID from the list above.")
        else:
            print(
                "  -> The model exists but chat requests are refused for your account. On NVIDIA this usually means\n"
                "     the key/account hasn't been enabled for the public API endpoints. Generate a new key at\n"
                "     build.nvidia.com (open the model page and use 'Get API Key'), or try a different model."
            )
    elif status in (401, 403):
        print("  -> The key was rejected or lacks access to this model.")
    elif status == 429:
        print("  -> Rate limited. Wait a minute and retry.")


def check_anthropic():
    import ai

    print(f"Provider: anthropic\nAI_MODEL: {config.AI_MODEL}\n")
    try:
        out = asyncio.run(ai.complete("Reply briefly.", [{"role": "user", "content": "Say ok"}], 20))
        print("Success. Model replied:", out[:80])
    except Exception as e:
        print(f"Failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    if config.AI_PROVIDER == "nvidia":
        check_nvidia(sys.argv[1] if len(sys.argv) > 1 else "")
    else:
        check_anthropic()
