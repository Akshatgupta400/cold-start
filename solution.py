#!/usr/bin/env python3
"""Cold Start: read one person's WhatsApp export and write output.json.

    python3 solution.py <input_dir> <output.json>

Stages (each falls back to rules if no model is configured or a call fails):
  1. structure  organisations, and which organisation each group chat belongs to   (1 call)
  2. people     circle, links and looks_after for everyone                          (~1 call per 22 people)
  3. loops      open loops in the scored window, their status, and where to help   (~1 call per 90 messages read)
  4. goals      goals with evidence                                                (a few calls)

Set NO_LLM=1 to run on rules alone.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm  # noqa: E402
from xpert import goals, loops, people, structure  # noqa: E402
from xpert.data import Data  # noqa: E402


def tie_chats(d, chat_org, circles):
    """Every chat to exactly one person or organisation.

    1:1 chats go to the other person. A group goes to its organisation when it has one; otherwise to the
    person it most belongs to: a family member if it is a family group, else whoever speaks most in it.
    """
    ties = []
    rank = {"child": 0, "spouse": 1, "parent": 2, "sibling": 3, "in-law": 4, "relative": 5}
    for c in d.chats:
        cid = c["chat_id"]
        others = [p for p in c["participants"] if p != "ME"]
        if cid in chat_org:
            ties.append({"chat_id": cid, "organisation": chat_org[cid]})
            continue
        if not others:
            continue  # cannot happen in valid input; skipped rather than invented
        if len(others) == 1:
            ties.append({"chat_id": cid, "person": others[0]})
            continue
        speak = {}
        for m in d.by_chat.get(cid, []):
            if m["sender_id"] != "ME":
                speak[m["sender_id"]] = speak.get(m["sender_id"], 0) + 1
        family = [p for p in others if circles.get(p, {}).get("circle") in rank]
        if family and len(family) >= len(others) / 2:
            best = min(family, key=lambda p: (rank[circles[p]["circle"]], -speak.get(p, 0)))
        else:
            best = max(others, key=lambda p: (speak.get(p, 0), p in d.dm_chat))
        ties.append({"chat_id": cid, "person": best})
    return ties


def assemble(d, orgs, ties, circles, goal_list, loop_list):
    used = {t["organisation"] for t in ties if "organisation" in t}
    used |= {l["organisation"] for r in circles.values() for l in r["links"]}
    orgs = [o for o in orgs if o["id"] in used]
    org_ids = {o["id"] for o in orgs}

    people_out = []
    for p in d.people:
        r = circles[p]
        e = {"id": p, "circle": r["circle"]}
        links = [l for l in r["links"] if l["organisation"] in org_ids]
        if links:
            e["links"] = links
        if r.get("looks_after"):
            e["looks_after"] = r["looks_after"]
        people_out.append(e)

    loops_out = []
    for l in loop_list:
        e = {k: l.get(k) for k in ("title", "type", "owner", "chat_id", "source_msg", "due", "status", "closed_by")}
        e["help"] = l.get("help") if l["status"] == "open" else None
        loops_out.append(e)

    return {"people": people_out, "organisations": orgs, "chats": ties, "goals": goal_list, "open_loops": loops_out}


def safely(name, fn, fallback):
    """Run a stage; on any error, log it and use the rule-based fallback instead."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - a failed stage must never cost the whole run
        print(f"[{name}] failed ({type(e).__name__}: {e}); using rules")
        return fallback()


def main(input_dir, output_path):
    started = time.time()
    d = Data(input_dir)
    use_llm = llm.available() and os.environ.get("NO_LLM") != "1"
    print(f"{len(d.messages)} messages, {len(d.chats)} chats, {len(d.people)} people. Model: "
          f"{os.environ.get('LLM_MODEL') if use_llm else 'none (rules only)'}")

    orgs, chat_org = safely("structure", lambda: structure.run(d, use_llm), lambda: structure.run(d, False))
    print(f"[{time.time() - started:5.0f}s] structure: {len(orgs)} organisations, {len(chat_org)} group chats assigned")

    circles = safely("people", lambda: people.run(d, orgs, chat_org, use_llm), lambda: people.run(d, orgs, chat_org, False))
    print(f"[{time.time() - started:5.0f}s] people: {len(circles)}")

    ties = tie_chats(d, chat_org, circles)

    loop_list, signals = safely("loops", lambda: loops.run(d, circles, orgs, chat_org, use_llm),
                                lambda: loops.run(d, circles, orgs, chat_org, False))
    print(f"[{time.time() - started:5.0f}s] loops: {len(loop_list)} ({sum(l['status'] == 'open' for l in loop_list)} open)")

    window_start = d.last_ts - loops.LOOP_DAYS * 86400
    goal_list = safely("goals", lambda: goals.run(d, signals, window_start, use_llm), lambda: [])
    print(f"[{time.time() - started:5.0f}s] goals: {len(goal_list)}")

    out = assemble(d, orgs, ties, circles, goal_list, loop_list)
    Path(output_path).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[{time.time() - started:5.0f}s] wrote {output_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
