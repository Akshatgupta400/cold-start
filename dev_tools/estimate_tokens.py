"""Estimate a run's input tokens without calling any model.

Runs the whole pipeline with a stub that records each prompt and returns nothing (so every stage
takes its rule fallback after the call). Prompt sizes are what a real run sends; output tokens are
not estimated.

    python3 dev_tools/estimate_tokens.py data/dev [chars_per_token=3.2]
"""
import collections
import contextlib
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import llm  # noqa: E402

folder = sys.argv[1]
cpt = float(sys.argv[2]) if len(sys.argv) > 2 else 3.2
chars, calls = collections.Counter(), collections.Counter()


def stub(system, user, max_tokens=4000, tag=""):
    t = tag.split(":")[0]
    chars[t] += len(system) + len(user)
    calls[t] += 1
    return {}


llm.chat_json = stub
os.environ.update({"LLM_BASE_URL": "x", "LLM_API_KEY": "x", "LLM_MODEL": "stub"})
import solution  # noqa: E402
from xpert.data import Data  # noqa: E402

with contextlib.redirect_stdout(io.StringIO()):
    solution.main(folder, os.devnull)
n_msgs = len(Data(folder).messages)
total = 0
print(f"{'stage':<14}{'calls':>6}{'input tokens':>14}")
for t in chars:
    tok = int(chars[t] / cpt)
    total += tok
    print(f"{t:<14}{calls[t]:>6}{tok:>14,}")
print(f"{'total':<14}{sum(calls.values()):>6}{total:>14,}   ({total / n_msgs * 1000:,.0f} per 1,000 messages)")
print("Goal-filter calls are undercounted here: with an empty stub there are no loop goal signals,")
print("but the filter pass itself is counted in full.")
