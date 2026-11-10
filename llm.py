"""A tiny client for any OpenAI-compatible chat API, standard library only.

It reads LLM_BASE_URL, LLM_API_KEY and LLM_MODEL from the environment. Every call appends its token
counts to usage.jsonl, so a run's cost is visible before the organisers measure it.

Additions over the starter version (same `chat()` signature, so it stays a drop-in):
  * retries with backoff on network errors, 429s and 5xx
  * a lock around usage.jsonl, because calls run in parallel threads
  * `chat_json()`: asks for JSON and parses it robustly, returning None on failure
  * an optional on-disk cache (LLM_CACHE=1) so re-running on the dev set during development is free.
    It is off by default, so a scoring run always makes real calls.
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

_usage_lock = threading.Lock()
# The run must finish in 30 minutes. After this budget no new call starts; stages fall back to rules.
_DEADLINE = time.time() + float(os.environ.get("RUN_BUDGET_SEC", str(25 * 60)))
CACHE_DIR = os.environ.get("LLM_CACHE_DIR", ".llm_cache")
MAX_RATE_WAIT_SEC = 120


def available():
    return bool(os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_API_KEY") and os.environ.get("LLM_MODEL"))


def _log_usage(model, usage, seconds, tag):
    with _usage_lock:
        with open("usage.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "model": model,
                "tag": tag,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "seconds": round(seconds, 2),
            }) + "\n")


def chat(messages, model=None, temperature=0, max_tokens=None, response_format=None, timeout=180, tag="", retries=5):
    if time.time() > _DEADLINE:
        raise RuntimeError("run time budget spent; skipping model call")
    base = os.environ["LLM_BASE_URL"].rstrip("/")
    body = {"model": model or os.environ["LLM_MODEL"], "messages": messages, "temperature": temperature}
    if max_tokens:
        body["max_tokens"] = max_tokens
    if response_format:
        body["response_format"] = response_format
    # Optional, for reasoning models: thinking counts against max_tokens and can leave an empty reply.
    # LLM_REASONING_EFFORT_<STAGE> (e.g. _STRUCTURE) overrides it for one stage.
    stage = re.sub(r"\W", "_", tag.split(":")[0]).upper()
    effort = os.environ.get(f"LLM_REASONING_EFFORT_{stage}") or os.environ.get("LLM_REASONING_EFFORT")
    if effort:
        body["reasoning_effort"] = effort

    cache_path = None
    if os.environ.get("LLM_CACHE") == "1":
        key = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
        os.makedirs(CACHE_DIR, exist_ok=True)
        cache_path = os.path.join(CACHE_DIR, key + ".json")
        if os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)["content"]

    last_err = None
    for attempt in range(retries + 1):
        if attempt and time.time() > _DEADLINE:
            break
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            # A named User-Agent: Cloudflare-fronted providers (Groq) answer 403 to urllib's default one.
            headers={
                "Authorization": "Bearer " + os.environ["LLM_API_KEY"],
                "Content-Type": "application/json",
                "User-Agent": "cold-start/1.0",
            },
        )
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            _log_usage(body["model"], data.get("usage") or {}, time.time() - started, tag)
            content = data["choices"][0]["message"]["content"] or ""
            if cache_path and content:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump({"content": content}, f)
            return content
        except urllib.error.HTTPError as e:
            last_err = e
            raw = e.read().decode("utf-8", "replace")
            detail = raw[:300]
            # A provider that rejects response_format: drop it and retry straight away.
            if e.code == 400 and "response_format" in body:
                body.pop("response_format")
                continue
            # Too large for a per-minute token limit (Groq counts input + max_tokens): shrink max_tokens to fit.
            if e.code == 413 and body.get("max_tokens"):
                m = re.search(r"Limit (\d+), Requested (\d+)", raw)
                if m:
                    limit, requested = int(m.group(1)), int(m.group(2))
                    room = limit - (requested - body["max_tokens"]) - 100
                    if 800 <= room < body["max_tokens"]:
                        body["max_tokens"] = room
                        continue
            if e.code not in (408, 409, 429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {e.code}: {detail}") from e
            if e.code == 429:
                try:
                    wait = float(e.headers.get("retry-after") or 0)
                except ValueError:
                    wait = 0
                # A long wait means a daily quota. Sleeping on it would eat the run budget, so fail fast.
                if wait > MAX_RATE_WAIT_SEC:
                    raise RuntimeError(f"HTTP 429, retry-after {wait:.0f}s: {detail}") from e
                if wait:
                    time.sleep(wait + 1)
                    continue
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError, KeyError) as e:
            last_err = e
        time.sleep(min(60, 2 ** attempt * 5))
    raise RuntimeError(f"LLM call failed after retries: {last_err}")


def parse_json(text):
    """The first JSON object in a reply, tolerating code fences and chatter around it."""
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def chat_json(system, user, max_tokens=4000, tag=""):
    """One call that should return a JSON object. None if the call or the parse fails."""
    try:
        reply = chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            tag=tag,
        )
    except Exception as e:  # noqa: BLE001 - any failure falls back to rules upstream
        print(f"[llm] {tag} failed: {e}")
        return None
    out = parse_json(reply)
    if not isinstance(out, dict):
        print(f"[llm] {tag}: reply was not a JSON object")
        return None
    return out


def as_dict(x):
    return x if isinstance(x, dict) else {}


def as_list(x):
    return x if isinstance(x, list) else []


def pick(x, allowed):
    """x if it is a string in allowed, else None. Guards lookups against lists or dicts from a reply."""
    return x if isinstance(x, str) and x in allowed else None
