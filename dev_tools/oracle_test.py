"""Plumbing test: replace the model with an oracle that answers from the dev answer key.

It checks that every stage parses replies, chunks chats, carries loops and assembles a valid output,
and shows the best score the post-processing allows. It says nothing about prompt quality.

    python3 dev_tools/oracle_test.py data/dev
"""
import json
import os
import subprocess
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import llm  # noqa: E402

folder = sys.argv[1]
ans = json.loads((Path(folder) / "answers" / "answers.json").read_text())
ans_circle = {p["id"]: p for p in ans["people"]}
org_of_chat = {c["chat_id"]: c["organisation"] for c in ans["chats"] if "organisation" in c}
calls = {"n": 0}


def fake(system, user, max_tokens=4000, tag=""):
    calls["n"] += 1
    if tag == "structure":
        ids = re.findall(r"^(C\d+) \|", user, re.M)
        return {"organisations": ans["organisations"], "chat_org": {c: org_of_chat.get(c) for c in ids}}
    if tag == "people":
        ids = re.findall(r"^## (P\d+)", user, re.M)
        return {"people": {p: {"circle": ans_circle[p]["circle"], "links": ans_circle[p].get("links", [])}
                           for p in ids if p in ans_circle}}
    if tag.startswith("loops:"):
        ids = set()
        for seg in user.split("NEW messages:")[1:]:
            seg = re.split(r"\n=== |\nEarlier messages", seg)[0]
            ids |= set(re.findall(r"^(M\d+) \|", seg, re.M))
        out = []
        for a in ans["open_loops"]:
            s = a["source_msgs"][0]
            if s in ids:
                out.append({"source_msg": s, "title": a["title"], "type": a["type"], "owner": a["owner"] or "ME",
                            "due": a["due"], "status": a["status"], "closed_by": None})
        return {"new_loops": out, "updates": [], "goal_signals": []}
    if tag == "help":
        n = len(re.findall(r"^X\d+ \|", user, re.M))
        return {"help": {f"X{i}": {"action": "remind", "remind_at": "2026-09-07T10:00:00+05:30"} for i in range(n)}}
    return {}


llm.chat_json = fake
os.environ.update({"LLM_BASE_URL": "x", "LLM_API_KEY": "x", "LLM_MODEL": "oracle"})
import solution  # noqa: E402

out = str(ROOT / "oracle_out.json")
solution.main(folder, out)
print("model calls:", calls["n"])
subprocess.run(["python3", str(ROOT / "check.py"), folder, out])
