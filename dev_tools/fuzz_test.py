"""Robustness: feed every stage malformed model replies and check the output stays valid.

    python3 dev_tools/fuzz_test.py data/dev
"""
import contextlib
import io
import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import check  # noqa: E402
import llm  # noqa: E402

JUNK = [None, 1, "x", [], {}, [1, 2], {"a": None}, "M00001", "P999", {"status": "done"}, "2026-13-45", True]


def junk(depth=0):
    r = random.random()
    if depth > 2 or r < 0.4:
        return random.choice(JUNK)
    if r < 0.7:
        return [junk(depth + 1) for _ in range(random.randint(0, 3))]
    keys = ["organisations", "chat_org", "people", "new_loops", "updates", "goal_signals", "help", "goals", "signals",
            "id", "circle", "links", "source_msg", "status", "closed_by", "ref", "msg", "action", "evidence", "area"]
    return {random.choice(keys): junk(depth + 1) for _ in range(random.randint(0, 4))}


def fake(system, user, max_tokens=4000, tag=""):
    return junk() if random.random() < 0.8 else None


llm.chat_json = fake
os.environ.update({"LLM_BASE_URL": "x", "LLM_API_KEY": "x", "LLM_MODEL": "fuzz"})
import solution  # noqa: E402

folder = sys.argv[1]
chats, messages, _, _ = check.load_input(folder)
for seed in range(25):
    random.seed(seed)
    out_path = "/tmp/fuzz_out.json"
    with contextlib.redirect_stdout(io.StringIO()):
        solution.main(folder, out_path)
    errs = check.validate(json.load(open(out_path)), chats, messages)
    if errs:
        print(f"seed {seed}: INVALID", errs[:3])
        sys.exit(1)
print("25 fuzzed runs, all outputs valid")
