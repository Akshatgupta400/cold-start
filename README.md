# Cold Start

Reads a few weeks of one person's WhatsApp and writes `output.json`: their people, organisations,
goals and open loops, and where an assistant should help.

```bash
export LLM_BASE_URL=...  LLM_API_KEY=...  LLM_MODEL=...
./run.sh data/dev out.json
python3 check.py data/dev out.json
```

Python 3.9+, standard library only. With no `LLM_*` variables (or `NO_LLM=1`) it runs on rules alone and
still writes a valid output.

## Layout

| Path | What it does |
|---|---|
| `run.sh` | The one command: `./run.sh <input_dir> <output.json>` |
| `solution.py` | Runs the four stages, ties chats, assembles the output. Any failing stage falls back to rules |
| `llm.py` | OpenAI-compatible client: retries, JSON parsing, token log in `usage.jsonl`, a 25-minute run budget |
| `xpert/data.py` | Loading and the shared views (per chat, per person, message formatting) |
| `xpert/rules.py` | Keyword lists and heuristics: the fallback, and hints for the model |
| `xpert/structure.py` | Stage 1: organisations and which group chat belongs to which (1 call) |
| `xpert/people.py` | Stage 2: circles, links, looks_after (batched evidence cards) |
| `xpert/loops.py` | Stage 3: open loops in the scored window, status, and help |
| `xpert/goals.py` | Stage 4: goals with evidence |
| `dev_tools/` | Tests and cost tools (below) |

## Dev tools

```bash
python3 dev_tools/estimate_tokens.py data/dev        # input tokens per stage, no model needed
python3 dev_tools/oracle_test.py data/dev            # plumbing test: model replaced by the answer key
python3 dev_tools/fuzz_test.py data/dev              # malformed model replies must still give valid output
python3 dev_tools/usage_report.py data/dev 0.10 0.40 # after a real run: cost per stage, $ per 1,000 messages
```

`LLM_WORKERS=1` runs one model call at a time, for providers with tight rate limits (like Groq's free tier).

`LLM_REASONING_EFFORT=low` sends `reasoning_effort` to reasoning models (e.g. `openai/gpt-oss-20b`); their thinking
counts against `max_tokens`, and at the default it can use them all and leave an empty reply.
`LLM_REASONING_EFFORT_STRUCTURE=medium` overrides it for one stage (here, the single organisations call).
Unset, nothing is sent, so other providers are unaffected. The dev runs used:

```bash
export LLM_BASE_URL=https://api.groq.com/openai/v1 LLM_MODEL=openai/gpt-oss-20b
export LLM_REASONING_EFFORT=low LLM_REASONING_EFFORT_STRUCTURE=medium
```

On a 429 the client waits as long as the provider asks; a wait over two minutes (a daily quota) fails the call
at once so the stage falls back to rules instead of spending the run budget. On a 413 for a per-minute token
limit it lowers `max_tokens` to fit and retries.

`LLM_CACHE=1` caches replies on disk, so re-running the dev set while changing post-processing is free.
Leave it off for a real measurement.

## Making a correction

Most corrections land in one place:

- "X is my driver, not a vendor": a word list in `xpert/rules.py` (`STAFF_WORDS`), or the circle definitions in `xpert/people.py`'s prompt.
- "That group is the society, not my company": the organisation rules in `xpert/structure.py`'s prompt.
- "Don't remind me about my wife's requests": the `leave` rules in `HELP_SYSTEM` in `xpert/loops.py`, and `rule_help` for the fallback.
- "You missed loops in group Y": the group filter in `select_messages` in `xpert/loops.py`.

Check with `oracle_test.py` (plumbing) and `check.py` (score) that nothing else moved.
