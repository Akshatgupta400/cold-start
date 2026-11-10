"""Stage 4: goals Arjun holds, each backed by messages.

Goal evidence can sit anywhere in the six weeks, while the loop pass reads only the last two. So:
1. Goal signals come free from the loop pass for the recent window.
2. For the earlier weeks, messages are filtered with a goal-word regex (only in chats Arjun takes part
   in, and his own lines in groups), and batches go to the model to keep only real goal signals.
3. One last call merges all signals into at most 8 goals. Invented goals cost points, so it is told
   to drop anything weakly supported.
"""
import concurrent.futures as cf
import os

import llm
from . import rules
from .data import clip, fmt_ts, msg_line

AREAS = ["self", "spouse", "kids", "parents", "work", "friends", "experiences", "finances", "health", "home"]

FILTER_SYSTEM = """You help a new assistant learn what {me} is working towards. Below are WhatsApp lines from his chats.
Pick only lines that show a GOAL of {me}'s own: something he is working towards over weeks, months or years - a fitness or health
target, a habit, a trip or experience to plan, a business milestone (clients, revenue, a launch, hiring, fundraising), an investment
or savings plan, something for his kids or parents, a home project. Ignore day-to-day errands, other people's goals, news and forwards.

Return JSON only: {{"signals": [{{"msg": "M..", "goal": "short description", "area": "{areas}"}}]}}"""

MERGE_SYSTEM = """You are building the goal list for {me}'s personal assistant. Below are signals, each tied to a message.
Merge them into AT MOST 8 goals {me} actually holds. Only keep a goal if the messages clearly show it; a weakly supported guess
costs more than a missing goal. Merge signals about the same goal. Do not repeat goals he already gave us: {seed}.

For each goal:
- title: short and specific ("Run the half marathon in January", "NEXUS to 50 paying D2C brands")
- area: one of {areas}
- type: habit (a rhythm, like yoga three times a week), milestone (a target), or experience (something to plan and do, like a trip)
- horizon: 3mo, 1yr, 3yr or 5yr
- evidence: the message ids that show it (from the signals; the most direct first)

Return JSON only: {{"goals": [{{"title": "...", "area": "...", "type": "...", "horizon": "...", "evidence": ["M.."]}}]}}"""


def candidate_lines(d, before_ts):
    out = []
    for m in d.messages:
        if m["ts"] >= before_ts:
            break
        c = d.chat_by_id[m["chat_id"]]
        body = m["body"] or ""
        if len(body) < 15 or body.startswith("http"):
            continue
        mine = m["sender_id"] == "ME"
        involved = (not c["is_group"]) or mine
        if involved and rules.matches(rules.GOAL_WORDS, body):
            out.append(m)
    return out


def run(d, loop_signals, window_start, use_llm, workers=None):
    workers = workers or int(os.environ.get("LLM_WORKERS", "6"))
    if not use_llm:
        return []
    areas = "|".join(AREAS)
    signals = [s for s in loop_signals if llm.pick(s.get("msg"), d.msg_by_id) is not None]

    cands = candidate_lines(d, window_start)
    system = FILTER_SYSTEM.format(me=d.me_name, areas=areas)
    batches = [cands[i:i + 220] for i in range(0, len(cands), 220)]

    def ask(batch):
        ids = {m["id"] for m in batch}
        res = llm.as_dict(llm.chat_json(system, "\n".join(msg_line(d, m, 260) for m in batch), max_tokens=2500, tag="goals-filter"))
        return [s for s in llm.as_list(res.get("signals")) if isinstance(s, dict) and llm.pick(s.get("msg"), ids) is not None]

    with cf.ThreadPoolExecutor(workers) as ex:
        for found in ex.map(ask, batches):
            signals += found
    if not signals:
        return []

    seen, lines = set(), []
    for s in signals:
        if s["msg"] in seen:
            continue
        seen.add(s["msg"])
        m = d.msg_by_id[s["msg"]]
        who = "ME" if m["sender_id"] == "ME" else d.names_of(m["sender_id"])[0]
        lines.append(f"{m['id']} | {fmt_ts(m['ts'])} | {who} in \"{d.chat_label(m['chat_id'])[:30]}\" | "
                     f"signal: {s.get('goal')} [{s.get('area')}] | text: {clip(m['body'], 200)}")
    seed = "; ".join(g.get("title", "") for g in d.seed.get("goals", [])) or "none"
    res = llm.as_dict(llm.chat_json(MERGE_SYSTEM.format(me=d.me_name, areas=areas, seed=seed), "\n".join(lines[:400]),
                                    max_tokens=2500, tag="goals-merge"))
    goals = []
    for g in llm.as_list(res.get("goals")):
        if not isinstance(g, dict):
            continue
        ev = [e for e in llm.as_list(g.get("evidence")) if isinstance(e, str) and e in d.msg_by_id]
        if not ev or llm.pick(g.get("area"), AREAS) is None:
            continue
        goals.append({
            "title": clip(str(g.get("title") or "Goal"), 120),
            "area": g["area"],
            "type": g.get("type") if g.get("type") in ("habit", "milestone", "experience") else "milestone",
            "horizon": g.get("horizon") if g.get("horizon") in ("3mo", "1yr", "3yr", "5yr") else "1yr",
            "evidence": ev[:6],
        })
    return goals[:8]
