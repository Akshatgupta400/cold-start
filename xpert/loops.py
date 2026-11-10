"""Stage 3: open loops, their status at the end of the data, and where an assistant should help.

How it reads the data
- Only the window that is scored is read for loops: the last LOOP_DAYS days (default 16, a little over
  the two weeks the hidden set is scored on). On the two-week dev set that is everything.
- Each chat is read in time order, in chunks. Loops still open after a chunk are carried into the
  next chunk of the same chat, so a later "paid", "done" or "never mind" can close them.
- Chats run in parallel; chunks within a chat run in sequence.
- Busy groups where Arjun hardly speaks are thinned to the lines that could matter to him (his own
  lines and the few before them, his name, birthdays), instead of being sent whole.

After all chunks, one batched pass decides help (assist / remind / leave) for every loop still open,
with the final status known.
"""
import concurrent.futures as cf
import datetime
import os
import re

import llm
from . import rules
from .data import IST, clip, fmt_ts, iso_date, msg_line

LOOP_DAYS = float(os.environ.get("LOOP_DAYS", "16"))
CHUNK_MSGS = 90
CHUNK_CHARS = 14000
LOOP_TYPES = {"commitment", "followup", "deadline", "question"}
GOAL_AREAS = {"self", "spouse", "kids", "parents", "work", "friends", "experiences", "finances", "health", "home"}

SYSTEM = """You are {me}'s new personal assistant, reading his WhatsApp to find OPEN LOOPS: things still in motion that he would want tracked.

Loop types:
- commitment: {me} promised or agreed to do something ("will send tomorrow", "I'll pay", "sure, will connect"), or someone close asked him to do something and it is his to do ("pls order the kids' watch", "pay 3500 on this QR").
- followup: {me} is waiting on someone else to deliver ("send me the quote", he told the broker to buy and waits for confirmation, a vendor needs measurements before quoting).
- deadline: something due on a specific date (a fee to pay by the 2nd, a form due Friday, a birthday wish due today).
- question: {me} must decide or answer something ("should I get the bigger rug?", "can we meet tomorrow 11-2?", "Bangalore or Zoom?").

Be selective. A loop needs a real action or answer, in this chat, that a sharp assistant would track. Skip small talk, jokes,
news, forwards, generic group announcements not aimed at {me}, rhetorical questions, and anything that only concerns other people.
In groups, a request counts only if it is addressed to {me} or clearly needs him. A birthday or big milestone of someone in a group he
is part of is a deadline loop for {me} to wish them (owner ME, due that day).

For each NEW loop:
- source_msg: the id of the message that raised it (the first ask or promise, not a later reminder). Must be one of the NEW messages.
- title: short, imperative, specific ("Pay Nayna ₹3,500 on the UPI QR").
- type, owner ("ME" or the person id who owes it), due (YYYY-MM-DD or null; resolve "tomorrow", "Saturday", "kal" from the message time).
- status at the end of what you can see: "done" only with a message that shows it happened (he says paid/sent/ordered/booked,
  the other side confirms receipt, a screenshot of payment followed by thanks, the meeting happened); "dropped" if it was called off or
  made irrelevant; otherwise "open". A bare "ok" is agreement, not completion. When unsure, say "open".
- closed_by: the message id that shows done/dropped, else null.

For loops carried over from earlier, report only those whose status changed in the new messages, under "updates".

Also note any GOAL signals: messages showing something {me} is working towards over weeks or months (a fitness target, a trip to plan,
a business target, an investment plan, something for the kids, a home project). Only his own goals, with the message id.

Return JSON only:
{{"new_loops": [{{"source_msg": "M..", "title": "...", "type": "...", "owner": "ME", "due": null, "status": "open", "closed_by": null}}],
  "updates": [{{"ref": "L1", "status": "done", "closed_by": "M.."}}],
  "goal_signals": [{{"msg": "M..", "goal": "...", "area": "self|spouse|kids|parents|work|friends|experiences|finances|health|home"}}]}}"""

HELP_SYSTEM = """You are {me}'s personal assistant. For each loop that is still OPEN, decide how you should help:

- assist: you can move it forward yourself, privately and reversibly: find options or prices, draft the reply or message, prepare a
  payment or an order for his approval, look up a slot, collect details. Give next_step in one line, and needs_yes = true if that step
  sends something, pays, orders, books, or cannot be undone (anything that leaves his phone).
- remind: only {me} can do it (a decision only he can make, a call or a visit, a conversation, a payment from his own account that you
  cannot prepare, a meeting he must attend). Give remind_at: a sensible ISO date-time in +05:30, before the due date when there is one.
- leave: stay out. Use it when someone else owns it and is handling it; when it is personal or emotional (his wife's or family's
  feelings, health, a talk they need to have, private family matters); or when a reminder would only annoy him (trivial, he is clearly
  already on it, it is a pleasantry). Give the reason in a few words.

Knowing when to stay quiet matters as much as helping: an assistant that pokes him about his wife's messages, or offers to help with
something his staff already handle, gets switched off. Loops owned by someone other than ME are usually "leave".

Return JSON only: {{"help": {{"<loop id>": {{"action": "assist", "next_step": "...", "needs_yes": true}} | {{"action": "remind", "remind_at": "2026-09-05T10:00:00+05:30"}} | {{"action": "leave", "reason": "..."}}}}}}"""


# ---------------------------------------------------------------- choosing what to read

def _is_active_group(d, c):
    """A group worth reading whole: Arjun speaks in it, and it is small or he is a regular there."""
    n = len(d.by_chat.get(c["chat_id"], []))
    me = d.me_count[c["chat_id"]]
    if me == 0:
        # Small groups are cheap and personal; read them unless they look like a bot feed.
        avg = sum(len(m["body"] or "") for m in d.by_chat.get(c["chat_id"], [])) / max(1, n)
        return len(c["participants"]) <= 6 and avg < 300
    return len(c["participants"]) <= 6 or me >= 5 or (n and me / n >= 0.05)


def select_messages(d, window_start):
    """Per chat: (context lines before the window, messages in the window to read)."""
    name_pat = re.compile(r"\b" + re.escape(d.me_first.lower()) + r"\b") if d.me_first else None
    plan = {}
    for c in d.chats:
        msgs = d.by_chat.get(c["chat_id"], [])
        win = [m for m in msgs if m["ts"] >= window_start]
        if not win:
            continue
        before = [m for m in msgs if m["ts"] < window_start][-8:]
        if c["is_group"] and not _is_active_group(d, c):
            keep = set()
            for i, m in enumerate(win):
                body = (m["body"] or "").lower()
                if m["sender_id"] == "ME":
                    keep.update(range(max(0, i - 3), i + 2))
                elif rules.matches(rules.BIRTHDAY_WORDS, body) or rules.matches(rules.DEADLINE_WORDS, body) \
                        or (name_pat and name_pat.search(body)):
                    keep.add(i)
            win = [win[i] for i in sorted(keep) if i < len(win)]
            before = []
            if not win:
                continue
        plan[c["chat_id"]] = (before, win)
    return plan


def chunks(msgs):
    out, cur, size = [], [], 0
    for m in msgs:
        n = min(len(m["body"] or ""), 350) + 40
        if cur and (len(cur) >= CHUNK_MSGS or size + n > CHUNK_CHARS):
            out.append(cur)
            cur, size = [], 0
        cur.append(m)
        size += n
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- reading one chat

def chat_header(d, cid, circles, orgs_by_id, chat_org):
    c = d.chat_by_id[cid]
    others = [p for p in c["participants"] if p != "ME"]
    def who(p):
        circ = circles.get(p, {}).get("circle")
        return f"{p} {d.names_of(p)[0]}" + (f" ({circ})" if circ else "")
    if not c["is_group"] and others:
        return f"1:1 chat {cid} between ME ({d.me_name}) and {who(others[0])}"
    org = orgs_by_id.get(chat_org.get(cid) or "")
    speakers = []
    for m in d.by_chat.get(cid, []):
        if m["sender_id"] != "ME" and m["sender_id"] not in speakers:
            speakers.append(m["sender_id"])
    listed = ", ".join(who(p) for p in speakers[:14])
    return (f"Group chat {cid} \"{d.chat_label(cid)}\" with {len(others)} others"
            + (f", part of his organisation {org['name']}" if org else "")
            + (f". People who speak: {listed}" if listed else ""))


def read_chat(d, cid, before, win, circles, orgs_by_id, chat_org, system):
    header = chat_header(d, cid, circles, orgs_by_id, chat_org)
    loops, signals, failed = [], [], []
    parts = chunks(win)
    for k, part in enumerate(parts):
        ids = {m["id"] for m in part}
        carried = [l for l in loops if l["status"] == "open"]
        lines = [header, f"Today, at the end of these messages, is {fmt_ts(part[-1]['ts'])} (+05:30)."]
        if carried:
            lines.append("\nLoops carried over from earlier in this chat:")
            for l in carried:
                lines.append(f"{l['ref']} | from {l['source_msg']} | {l['type']} | owner {l['owner']} | due {l['due']} | {l['title']}")
        ctx = before if k == 0 else parts[k - 1][-6:]
        if ctx:
            lines.append("\nEarlier messages, for context only (do not create loops from these):")
            lines += [msg_line(d, m, 200) for m in ctx]
        lines.append("\nNEW messages:")
        lines += [msg_line(d, m) for m in part]
        raw = llm.chat_json(system, "\n".join(lines), max_tokens=3500, tag=f"loops:{cid}")
        if raw is None:
            failed = [cid]
        res = llm.as_dict(raw)

        by_ref = {l["ref"]: l for l in loops}
        # Models often name a carried loop by its source message id instead of its ref.
        by_ref.update({l["source_msg"]: l for l in loops if l["source_msg"] not in by_ref})
        for u in llm.as_list(res.get("updates")):
            u = llm.as_dict(u)
            l = by_ref.get(str(u.get("ref")))
            if l and u.get("status") in ("done", "dropped", "open"):
                l["status"] = u["status"]
                l["closed_by"] = u.get("closed_by") if llm.pick(u.get("closed_by"), d.msg_by_id) is not None else None
        for n in llm.as_list(res.get("new_loops")):
            if not isinstance(n, dict) or llm.pick(n.get("source_msg"), ids) is None:
                continue
            loops.append(_new_loop(d, n, cid, len(loops)))
        for g in llm.as_list(res.get("goal_signals")):
            if isinstance(g, dict) and llm.pick(g.get("msg"), ids) is not None:
                signals.append({"msg": g["msg"], "goal": clip(str(g.get("goal") or ""), 120),
                                "area": g.get("area") if llm.pick(g.get("area"), GOAL_AREAS) is not None else None})
    return loops, signals, failed


def read_pack(d, cids, plan, circles, orgs_by_id, chat_org, system):
    """Several small chats in one call. They need no carry-over: each fits in a single chunk."""
    lines = [f"Several separate chats follow. Today, at the end of the data, is {fmt_ts(d.last_ts)} (+05:30)."]
    chat_of = {}
    for cid in cids:
        before, win = plan[cid]
        lines.append("\n=== " + chat_header(d, cid, circles, orgs_by_id, chat_org))
        if before:
            lines.append("Earlier messages, for context only (do not create loops from these):")
            lines += [msg_line(d, m, 200) for m in before[-4:]]
        lines.append("NEW messages:")
        for m in win:
            lines.append(msg_line(d, m))
            chat_of[m["id"]] = cid
    raw = llm.chat_json(system, "\n".join(lines), max_tokens=3500, tag="loops:pack")
    res = llm.as_dict(raw)
    loops, signals = [], []
    for n in llm.as_list(res.get("new_loops")):
        if not isinstance(n, dict) or llm.pick(n.get("source_msg"), chat_of) is None:
            continue
        loops.append(_new_loop(d, n, chat_of[n["source_msg"]], len(loops)))
    for g in llm.as_list(res.get("goal_signals")):
        if isinstance(g, dict) and llm.pick(g.get("msg"), chat_of) is not None:
            signals.append({"msg": g["msg"], "goal": clip(str(g.get("goal") or ""), 120),
                            "area": g.get("area") if llm.pick(g.get("area"), GOAL_AREAS) is not None else None})
    return loops, signals, (list(cids) if raw is None else [])


def _new_loop(d, n, cid, i):
    return {
        "ref": f"L{i + 1}",
        "chat_id": cid,
        "source_msg": n["source_msg"],
        "title": clip(str(n.get("title") or "Open item"), 140),
        "type": n.get("type") if llm.pick(n.get("type"), LOOP_TYPES) is not None else "commitment",
        "owner": n.get("owner") if isinstance(n.get("owner"), str) else "ME",
        "due": n.get("due") if _is_date(n.get("due")) else None,
        "status": n.get("status") if n.get("status") in ("open", "done", "dropped") else "open",
        "closed_by": n.get("closed_by") if llm.pick(n.get("closed_by"), d.msg_by_id) is not None else None,
    }


def _is_date(s):
    return isinstance(s, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))


# ---------------------------------------------------------------- after reading

def tidy(d, loops):
    """Make every loop consistent with the data, and drop duplicates."""
    out, seen = [], set()
    people = set(d.people)
    for l in loops:
        key = (l["source_msg"], l["title"].lower()[:30])
        if key in seen:
            continue
        seen.add(key)
        if l["owner"] != "ME" and l["owner"] not in people:
            l["owner"] = "ME"
        cb = l.get("closed_by")
        if cb:
            src, close = d.msg_by_id[l["source_msg"]], d.msg_by_id.get(cb)
            if not close or close["chat_id"] != l["chat_id"] or close["ts"] < src["ts"]:
                l["closed_by"] = None
        if l["status"] == "open":
            l["closed_by"] = None
        out.append(l)
    return out


def _default_remind(d, l):
    if l.get("due"):
        day = datetime.datetime.strptime(l["due"], "%Y-%m-%d").replace(tzinfo=IST)
        when = day.replace(hour=9, minute=30)
    else:
        src = datetime.datetime.fromtimestamp(d.msg_by_id[l["source_msg"]]["ts"], IST)
        when = (src + datetime.timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    return when.isoformat()


def rule_help(d, l, circles):
    """The fallback help call: stay out of others' loops and the spouse's personal ones."""
    if l["owner"] != "ME":
        return {"action": "leave", "reason": "someone else owns it"}
    other = [p for p in d.chat_by_id[l["chat_id"]]["participants"] if p != "ME"]
    if len(other) == 1 and circles.get(other[0], {}).get("circle") in ("spouse", "parent", "sibling", "child") \
            and re.search(r"\b(talk|discuss|feel|sad|upset|hard time|sorry|love|miss)\b", l["title"].lower()):
        return {"action": "leave", "reason": "personal family matter"}
    if re.search(r"\b(find|look|search|order|buy|book|draft|send|share|get)\b", l["title"].lower()):
        return {"action": "assist", "next_step": "Prepare it for his approval: " + l["title"], "needs_yes": True}
    return {"action": "remind", "remind_at": _default_remind(d, l)}


def decide_help(d, loops, circles, use_llm):
    open_loops = [l for l in loops if l["status"] == "open"]
    for l in open_loops:
        l["help"] = rule_help(d, l, circles)
    if not use_llm or not open_loops:
        return
    system = HELP_SYSTEM.format(me=d.me_name)
    batches = [open_loops[i:i + 40] for i in range(0, len(open_loops), 40)]

    def ask(batch):
        lines = []
        for i, l in enumerate(batch):
            src = d.msg_by_id[l["source_msg"]]
            c = d.chat_by_id[l["chat_id"]]
            others = [p for p in c["participants"] if p != "ME"]
            if c["is_group"]:
                where = f"group \"{d.chat_label(c['chat_id'])[:40]}\""
            else:
                p = others[0] if others else None
                where = f"1:1 with {d.names_of(p)[0]} ({circles.get(p, {}).get('circle', '?')})" if p else "1:1"
            owner = "ME" if l["owner"] == "ME" else f"{d.names_of(l['owner'])[0]} ({circles.get(l['owner'], {}).get('circle', '?')})"
            lines.append(f"X{i} | {l['type']} | owner {owner} | due {l['due']} | in {where} | {l['title']}\n"
                         f"     source ({fmt_ts(src['ts'])}): {clip(src['body'], 220)}")
        user = f"The data ends {fmt_ts(d.last_ts)} (+05:30).\n\nOpen loops:\n" + "\n".join(lines)
        return batch, llm.as_dict(llm.chat_json(system, user, max_tokens=3500, tag="help"))

    with cf.ThreadPoolExecutor(min(4, int(os.environ.get("LLM_WORKERS", "4")))) as ex:
        for batch, res in ex.map(ask, batches):
            got = llm.as_dict(llm.as_dict(res).get("help"))
            for i, l in enumerate(batch):
                h = got.get(f"X{i}")
                if not isinstance(h, dict) or h.get("action") not in ("assist", "remind", "leave"):
                    continue
                if h["action"] == "assist":
                    l["help"] = {"action": "assist", "next_step": clip(str(h.get("next_step") or l["title"]), 160),
                                 "needs_yes": bool(h.get("needs_yes", True))}
                elif h["action"] == "remind":
                    at = h.get("remind_at")
                    l["help"] = {"action": "remind", "remind_at": at if isinstance(at, str) and re.match(r"\d{4}-\d{2}-\d{2}T", at) else _default_remind(d, l)}
                else:
                    l["help"] = {"action": "leave", "reason": clip(str(h.get("reason") or "not his to handle"), 120)}


# ---------------------------------------------------------------- no-model fallback

def rule_loops(d, window_start, circles):
    """Without a model: requests to him in 1:1 chats, and his own promises. Crude but valid."""
    loops = []
    for c in d.chats:
        if c["is_group"]:
            continue
        msgs = [m for m in d.by_chat.get(c["chat_id"], []) if m["ts"] >= window_start]
        last_key = None
        for i, m in enumerate(msgs):
            body = (m["body"] or "").lower()
            if len(body) < 8 or body.startswith("http"):
                continue
            if m["sender_id"] != "ME" and rules.matches(rules.REQUEST_TO_ME, body):
                kind, owner = ("question" if "?" in body else "commitment"), "ME"
            elif m["sender_id"] == "ME" and rules.matches(rules.ME_PROMISE, body):
                kind, owner = "commitment", "ME"
            else:
                continue
            key = (m["sender_id"], m["ts"] // 1800)
            if key == last_key:
                continue  # one loop per burst of messages
            last_key = key
            loops.append({"ref": f"R{len(loops)}", "chat_id": c["chat_id"], "source_msg": m["id"],
                          "title": clip(m["body"], 100), "type": kind, "owner": owner, "due": None,
                          "status": "open", "closed_by": None})
    return loops


# ---------------------------------------------------------------- stage entry

def run(d, circles, orgs, chat_org, use_llm, workers=None):
    workers = workers or int(os.environ.get("LLM_WORKERS", "8"))
    window_start = d.last_ts - LOOP_DAYS * 86400
    if not use_llm:
        loops = tidy(d, rule_loops(d, window_start, circles))
        decide_help(d, loops, circles, False)
        return loops, []
    orgs_by_id = {o["id"]: o for o in orgs}
    plan = select_messages(d, window_start)
    system = SYSTEM.format(me=d.me_name)
    loops, signals, failed = [], [], set()
    # Long chats are read alone, chunk by chunk; short ones are packed several to a call.
    big = [cid for cid in plan if len(chunks(plan[cid][1])) > 1 or len(plan[cid][1]) > CHUNK_MSGS // 2]
    small = sorted((cid for cid in plan if cid not in big), key=lambda cid: -len(plan[cid][1]))
    packs, cur, size = [], [], 0
    for cid in small:
        n = sum(min(len(m["body"] or ""), 350) + 40 for m in plan[cid][1]) + 300
        if cur and size + n > CHUNK_CHARS:
            packs.append(cur)
            cur, size = [], 0
        cur.append(cid)
        size += n
    if cur:
        packs.append(cur)
    # Biggest chats first, so the slowest sequential chain starts early.
    big.sort(key=lambda cid: -len(plan[cid][1]))
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(read_chat, d, cid, plan[cid][0], plan[cid][1], circles, orgs_by_id, chat_org, system) for cid in big]
        futs += [ex.submit(read_pack, d, p, plan, circles, orgs_by_id, chat_org, system) for p in packs]
        for f in futs:
            try:
                l, s, bad = f.result()
                loops += l
                signals += s
                failed.update(bad)
            except Exception as e:  # noqa: BLE001 - one bad chat must not sink the run
                print(f"[loops] a chat failed: {e}")
    if failed:
        # A chat whose model call failed (quota, timeout, bad reply) gets the rule loops instead of none.
        have = {l["source_msg"] for l in loops}
        loops += [l for l in rule_loops(d, window_start, circles) if l["chat_id"] in failed and l["source_msg"] not in have]
        print(f"[loops] {len(failed)} chats fell back to rules")
    loops = [l for l in tidy(d, loops) if aimed_at_me(d, l)]
    decide_help(d, loops, circles, True)
    return loops, signals


def aimed_at_me(d, l):
    """In a busy group he rarely speaks in, keep a loop only if it is clearly his.

    The prompt already asks for this, but small models still log staff asking each other to update
    payment statuses. Kept: his own messages, ones naming him, birthdays, and dated deadlines.
    """
    c = d.chat_by_id[l["chat_id"]]
    if not c["is_group"] or _is_active_group(d, c):
        return True
    m = d.msg_by_id[l["source_msg"]]
    body = (m["body"] or "").lower()
    if m["sender_id"] == "ME" or rules.matches(rules.BIRTHDAY_WORDS, body):
        return True
    if d.me_first and re.search(r"\b" + re.escape(d.me_first.lower()) + r"\b", body):
        return True
    return l["type"] == "deadline" and bool(l["due"])


def window_info(d):
    return iso_date(d.last_ts - LOOP_DAYS * 86400), iso_date(d.last_ts)
