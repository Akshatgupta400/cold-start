"""Stage 1: Arjun's organisations, and which organisation each group chat belongs to.

One model call over a compact directory of every group chat (name, size, how active Arjun is, a few
sample lines). 1:1 chats are always tied to the person, so they are not sent.
"""
import re

import llm
from . import rules
from .data import clip

ORG_TYPES = {"company", "residence", "vacation home", "workspace", "society", "school", "other"}
ORG_ROLES = {"owner", "partner", "helper", "resident", "parent", "member", "other"}

SYSTEM = """You are setting up a personal assistant for {me}. You see a directory of his WhatsApp group chats.
Work out his ORGANISATIONS and which group chat belongs to which.

An organisation is something {me} runs or belongs to, with its own people and chats:
- his companies (type company, side work, role owner if he founded/runs it, partner if he co-owns it, helper if he only helps someone else's business, member otherwise)
- a separate office/workspace with its own staff (type workspace, side work)
- his home (type residence, side home) - chats with house staff, home maintenance, the kitchen, the building
- a farmhouse or vacation home (type vacation home, side home)
- a housing society (type society, side home), the kids' school (type school, side home, role parent)

Rules:
- A CLIENT's company is NOT an organisation. A group like "<Client>: <HisCompany> - Ad Targeting" or "<Client> x <HisCompany>" belongs to HIS company that serves the client.
- Communities, forums or circles that his company runs or hosts belong to that company.
- Family groups, friends' groups, hobby/sports groups, alumni groups, trips and general communities he merely joined belong to NO organisation: use null.
- Keep the list short: usually 3 to 8 organisations. Never create an organisation for a single social group.
- Group names often use initials or short forms of an organisation ("PMA" for "PostMyAd"); match those too.
- He already told us these organisation names: {seed}. Use them (exact names) and add others only when the chats clearly show them.

Return JSON only:
{{"organisations": [{{"id": "O1", "name": "...", "type": "...", "side": "work|home", "role": "..."}}],
  "chat_org": {{"C01": "O1", "C02": null, ...}}}}
Give chat_org for EVERY chat id listed."""


def directory(d):
    lines = []
    for c in d.chats:
        if not c["is_group"]:
            continue
        msgs = d.by_chat.get(c["chat_id"], [])
        sample = [m for m in msgs if m["kind"] == "text" and len(m["body"]) > 15][:3]
        snippet = " || ".join(clip(m["body"], 90) for m in sample)
        lines.append(
            f"{c['chat_id']} | \"{d.chat_label(c['chat_id'])}\" | {len(c['participants'])} people | "
            f"{len(msgs)} msgs, {d.me_count[c['chat_id']]} by {d.me_first} | {snippet}"
        )
    return lines


def _clean(result, d, fallback_orgs, fallback_map):
    orgs, seen = [], set()
    for o in llm.as_list(result.get("organisations")):
        if not isinstance(o, dict):
            continue
        oid = re.sub(r"[^A-Za-z0-9_-]", "", str(o.get("id") or "")) or f"O{len(orgs) + 1}"
        if oid in seen:
            continue
        seen.add(oid)
        orgs.append({
            "id": oid,
            "name": str(o.get("name") or oid)[:80],
            "type": o.get("type") if llm.pick(o.get("type"), ORG_TYPES) is not None else "other",
            "side": o.get("side") if o.get("side") in ("home", "work") else "work",
            "role": o.get("role") if llm.pick(o.get("role"), ORG_ROLES) is not None else "owner",
        })
    if not orgs:
        return fallback_orgs, fallback_map
    raw = llm.as_dict(result.get("chat_org"))
    return orgs, {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def run(d, use_llm):
    fb_orgs, fb_map = rules.heuristic_structure(d)
    if not use_llm:
        return fb_orgs, fb_map
    lines = directory(d)
    system = SYSTEM.format(me=d.me_name, seed=", ".join(d.seed["organisations"]) or "none")
    # One call normally; split only for unusually many groups, carrying the org list forward.
    orgs, chat_org = None, {}
    step = 220
    for i in range(0, len(lines), step):
        part = "\n".join(lines[i:i + step])
        user = "Group chats (id | name | size | activity | sample lines):\n" + part
        if orgs:
            user += "\n\nOrganisations already defined (reuse these ids, add new ones only if needed):\n" + \
                "\n".join(f"{o['id']}: {o['name']} ({o['type']}, {o['side']}, {o['role']})" for o in orgs)
        res = llm.as_dict(llm.chat_json(system, user, max_tokens=12000, tag="structure"))
        if not res:
            return fb_orgs, fb_map  # failed or empty reply: keep the rules
        new_orgs, new_map = _clean(res, d, fb_orgs, fb_map)
        if orgs:
            known = {o["id"] for o in orgs}
            orgs += [o for o in new_orgs if o["id"] not in known]
        else:
            orgs = new_orgs
        chat_org.update(new_map)
    ids = {o["id"] for o in orgs}
    groups = {c["chat_id"] for c in d.chats if c["is_group"]}
    chat_org = {k: v for k, v in chat_org.items() if v in ids and k in groups}
    return _merge_rules(orgs, chat_org, fb_orgs, fb_map)


def _merge_rules(orgs, chat_org, fb_orgs, fb_map):
    """Keep what the rules found when the model missed it.

    Small models drop organisations the user named in the seed (e.g. "PMA ..." groups for PostMyAd),
    so every rule organisation is kept (matched by name), and a chat the model left unassigned takes
    the rules' assignment. The model still wins wherever it assigned a chat.
    """
    by_name = {o["name"].strip().lower(): o["id"] for o in orgs}
    ids = {o["id"] for o in orgs}
    fb_to_id = {}
    for o in fb_orgs:
        oid = by_name.get(o["name"].strip().lower())
        if not oid:
            oid = o["id"]
            while oid in ids:
                oid += "_r"
            orgs.append(dict(o, id=oid))
            ids.add(oid)
        fb_to_id[o["id"]] = oid
    for chat, fb in fb_map.items():
        if chat not in chat_org and fb in fb_to_id:
            chat_org[chat] = fb_to_id[fb]
    return orgs, chat_org
