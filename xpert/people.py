"""Stage 2: each person's circle, their links to organisations, and who staff look after.

People are sent to the model in batches with a compact evidence card: their names, their groups (with
the organisation each group belongs to), a spread of lines from their 1:1 chat, and a few lines they
wrote in groups. People who never wrote and have no 1:1 chat get the rule-based guess: there is
nothing for a model to read, and they are rarely the ones Arjun places himself.
"""
import concurrent.futures as cf
import os

import llm
from . import rules
from .data import clip

CIRCLES = ["spouse", "child", "parent", "in-law", "sibling", "relative", "friend", "colleague", "client", "vendor", "partner", "staff"]

SYSTEM = """You are setting up a personal assistant for {me}. For each person below, decide their ONE circle: their relationship to {me}.

Circles:
- spouse, child, parent, in-law (spouse's family: father/mother/brother/sister-in-law), sibling, relative (any other family: cousins, uncles, aunts, nephews, the extended family)
- friend: personal friends, including friends in sports/hobby/alumni groups
- colleague: works WITH {me} in one of his organisations (co-workers, employees, team members, interns)
- partner: a business partner, co-founder, investor or someone he runs a venture with as an equal
- client: a customer of one of his companies (client-side people in "<Client>: <Company>" groups)
- vendor: someone {me} pays for goods or services (shops, contractors, coaches, teachers, brokers, consultants)
- staff: people employed by him personally or by his home/office: cook, maid, driver, cleaner, office helper, guard

A circle is about the role, not closeness. Family members are family even in work groups. Use names too:
{me}'s surname is "{surname}", so other people with that surname are usually family. Saved names often carry a tag
("Ramesh Driver", "Priya PMA", "Sharma Plumber") - trust them.

His organisations:
{orgs}

People he has already placed (use them to calibrate, e.g. family surnames):
{known}

Also give:
- links: only for colleague/partner (link "runs": they run that organisation with him) and staff (link "supports":
  they work for it, e.g. the home's cook supports the Home organisation). Use the organisation ids above. Empty otherwise.
- looks_after: for staff who look after one specific person (a driver or nanny for a child), that person's id. Usually empty.

Return JSON only: {{"people": {{"P01": {{"circle": "...", "links": [{{"organisation": "O1", "link": "runs"}}], "looks_after": []}}, ...}}}}"""


def evidence_card(d, pid, chat_org, orgs_by_id, hint):
    names = " / ".join(d.names_of(pid))
    groups = []
    for g in d.groups_of.get(pid, [])[:8]:
        o = chat_org.get(g)
        tag = f" [{orgs_by_id[o]['name']}]" if o in orgs_by_id else ""
        groups.append(f"\"{d.chat_label(g)[:40]}\"{tag}")
    lines = [f"## {pid} | names: {names} | sent {d.sent[pid]} msgs | rule guess: {hint}"]
    if groups:
        lines.append("groups: " + ", ".join(groups))
    dm = d.dm_chat.get(pid)
    if dm:
        msgs = [m for m in d.by_chat.get(dm, []) if m["body"] and m["body"] != "[photo]"]
        if len(msgs) > 12:
            # A spread across the chat says more than its first dozen lines.
            step = len(msgs) / 12
            msgs = [msgs[int(i * step)] for i in range(12)]
        lines.append(f"1:1 chat ({len(d.by_chat.get(dm, []))} msgs):")
        for m in msgs:
            who = "ME" if m["sender_id"] == "ME" else "THEM"
            lines.append(f"  {who}: {clip(m['body'], 140)}")
    gm = [m for g in d.groups_of.get(pid, []) for m in d.by_chat.get(g, [])
          if m["sender_id"] == pid and len(m["body"]) > 12][:4]
    for m in gm:
        lines.append(f"  in \"{d.chat_label(m['chat_id'])[:25]}\": {clip(m['body'], 120)}")
    return "\n".join(lines)


def _valid(entry, orgs_by_id, people):
    circle = entry.get("circle")
    if not isinstance(circle, str) or circle not in CIRCLES:
        return None
    links = []
    for l in llm.as_list(entry.get("links")):
        if isinstance(l, dict) and llm.pick(l.get("organisation"), orgs_by_id) and l.get("link") in ("runs", "supports"):
            if l not in links:
                links.append({"organisation": l["organisation"], "link": l["link"]})
    looks = [p for p in llm.as_list(entry.get("looks_after")) if isinstance(p, str) and p in people]
    return {"circle": circle, "links": links, "looks_after": looks}


def run(d, orgs, chat_org, use_llm, batch_size=22, workers=None):
    workers = workers or int(os.environ.get("LLM_WORKERS", "6"))
    orgs_by_id = {o["id"]: o for o in orgs}
    hints = {p: rules.heuristic_circle(d, p, chat_org, orgs_by_id) for p in d.people}
    result = {p: {"circle": hints[p], "links": [], "looks_after": []} for p in d.people}

    to_ask = [p for p in d.people if p not in d.seed["circles"] and (d.sent[p] > 0 or p in d.dm_chat)]
    # Most informative first, so batches are balanced in size.
    to_ask.sort(key=lambda p: -(d.sent[p] + 50 * (p in d.dm_chat)))

    if use_llm and to_ask:
        surname = d.me_name.split()[-1] if len(d.me_name.split()) > 1 else ""
        known = "\n".join(f"- {p} {d.names_of(p)[0]}: {c}" for p, c in d.seed["circles"].items() if p in result)
        system = SYSTEM.format(
            me=d.me_name, surname=surname,
            orgs="\n".join(f"- {o['id']}: {o['name']} ({o['type']}, {o['side']}, his role: {o['role']})" for o in orgs) or "- none",
            known=known or "- none",
        )
        batches = [to_ask[i:i + batch_size] for i in range(0, len(to_ask), batch_size)]

        def ask(batch):
            cards = "\n\n".join(evidence_card(d, p, chat_org, orgs_by_id, hints[p]) for p in batch)
            return batch, llm.chat_json(system, "People:\n\n" + cards, max_tokens=3000, tag="people")

        with cf.ThreadPoolExecutor(workers) as ex:
            for batch, res in ex.map(ask, batches):
                got = llm.as_dict(llm.as_dict(res).get("people"))
                for p in batch:
                    v = _valid(llm.as_dict(got.get(p)), orgs_by_id, set(d.people))
                    if v:
                        result[p] = v

    rules.family_cues(d, result)
    for p, c in d.seed["circles"].items():
        if p in result:
            result[p]["circle"] = c
    _fix_links(d, result, chat_org, orgs_by_id)
    return result


def _fix_links(d, result, chat_org, orgs_by_id):
    """Keep links only where the evidence is direct.

    Links count towards the organisation match, so linking every member of a 30-person ops group
    would swamp it. A colleague or partner is linked when Arjun deals with them directly (a 1:1 chat,
    or he placed them himself); staff are always linked, to their home or office.
    """
    for p, r in result.items():
        circle = r["circle"]
        small_team = any(chat_org.get(g) in orgs_by_id and len(d.chat_by_id[g]["participants"]) <= 8
                         for g in d.groups_of.get(p, []))
        direct = p in d.dm_chat or p in d.seed["circles"] or small_team
        if circle in ("colleague", "partner"):
            if not direct:
                r["links"] = []
                continue
            if not r["links"]:
                work = []
                for g in d.groups_of.get(p, []):
                    o = chat_org.get(g)
                    if o in orgs_by_id and orgs_by_id[o]["side"] == "work" and o not in work:
                        work.append(o)
                r["links"] = [{"organisation": o, "link": "runs"} for o in work]
            r["links"] = [l for l in r["links"] if l["link"] == "runs"] or r["links"]
        elif circle == "staff":
            if not r["links"]:
                homes = [o for o in orgs_by_id.values() if o["side"] == "home"]
                own = [chat_org.get(g) for g in d.groups_of.get(p, []) if chat_org.get(g) in orgs_by_id]
                target = own[0] if own else (homes[0]["id"] if homes else None)
                if target:
                    r["links"] = [{"organisation": target, "link": "supports"}]
            r["links"] = [{"organisation": l["organisation"], "link": "supports"} for l in r["links"]]
        else:
            r["links"] = []
            r["looks_after"] = []
