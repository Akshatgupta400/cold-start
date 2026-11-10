"""Deterministic rules: keyword lists and heuristics.

They do two jobs. They are the fallback when no model is configured or a call fails, so run.sh always
writes a valid output. And their guesses are passed to the model as hints, which is cheaper than
making the model rediscover them.

Everything here is a plain list or a small function, so a correction ("Raju is my driver, not a
vendor") usually means editing one list.
"""
import re

# ---------------------------------------------------------------- words

HOME_WORDS = r"\b(home|house|ghar|kitchen|maid|cook|driver|carpool|flat|building|society|apartment|residence|villa|bungalow)\b"
FARM_WORDS = r"\b(farm|farmhouse|vacation|holiday home|weekend home)\b"
SCHOOL_WORDS = r"\b(school|class \d|grade \d|parents'? group|ptm|section [a-f])\b"
FAMILY_WORDS = r"\b(family|siblings?|sisters?|brothers?|bro|sis|cousins?|khandan|parivar|in-?laws|mama|chacha|bua)\b"
STAFF_WORDS = r"\b(maid|cook|driver|didi|bhaiya|helper|cleaner|guard|watchman|peon|office boy|gardener|mali|nanny|ayah|home)\b"
VENDOR_WORDS = r"\b(traders?|trading|enterprises?|store|shop|agency|services|suppliers?|contractor|electrician|plumber|carpenter|tailor|sir|coach|teacher|tutor|yoga|gym|clinic|dr\.?|doctor|pharmacy|chemist|broker|ca|accountant)\b"
CLIENT_GROUP = r"^(.+?)\s*(?::|x|<>|-|\|)\s*(.+)$"

DEADLINE_WORDS = r"(\bpay\b|payment|\bfees?\b|confirm your|rsvp|register|last date|deadline|\bdue\b|by \d{1,2}(st|nd|rd|th)?\b|renew)"
BIRTHDAY_WORDS =r"(birthday|bday|b'day|anniversary|congrat|janamdin|happy \d+(st|nd|rd|th))"

GOAL_WORDS = (
    r"(goal|target|plan to|planning to|want to|wanna|aim|dream|this year|next year|by (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|marathon|\b\d+k run|half marathon|gym|workout|fitness|weight|diet|yoga|meditat|sleep|health|"
    r"trip|travel|vacation|holiday|visit|tour|trek|"
    r"invest|portfolio|savings|stock|mutual fund|sip|corpus|retire|"
    r"launch|revenue|arr|mrr|clients|customers|hire|hiring|fundrais|raise|valuation|scale|grow|"
    r"admission|school|exam|tuition|piano|learn|course|class|"
    r"renovat|interior|new house|construction|farm)"
)

REQUEST_TO_ME = r"\b(pls|plz|please|can u|can you|could you|kindly|need you to|send me|let me know|lmk|call me)\b|\?\s*$"
ME_PROMISE = r"\b(i'?ll|ill|i will|will do|will send|will check|will call|will share|will revert|reverting|tomorrow|kal|by (mon|tue|wed|thu|fri|sat|sun)|let me)\b"


def matches(pattern, text):
    return bool(re.search(pattern, (text or "").lower()))


# ---------------------------------------------------------------- organisations

def name_tokens(org_name):
    """Ways a chat name can refer to an organisation: the name, its acronym, its domain stem.

    "PostMyAd" -> {"postmyad", "pma"}; "NEXUS" -> {"nexus"}.
    """
    name = org_name.strip()
    toks = {name.lower(), name.lower().replace(" ", "")}
    caps = "".join(ch for ch in name if ch.isupper())
    if 2 <= len(caps) <= 5 and caps != name:
        toks.add(caps.lower())
    words = re.findall(r"[A-Za-z]+", name)
    if len(words) > 1:
        toks.add("".join(w[0] for w in words).lower())
    return {t for t in toks if len(t) >= 3}


def chat_mentions_org(chat_name, tokens):
    n = (chat_name or "").lower()
    return any(re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", n) for t in tokens)


def heuristic_structure(d):
    """Organisations and group-chat assignment without a model.

    Seed organisations are matched by name or acronym in chat names; home and farm by keywords.
    Everything else is tied to a person later.
    """
    orgs = []
    chat_org = {}
    for i, name in enumerate(d.seed.get("organisations", []), start=1):
        oid = f"O{i}"
        orgs.append({"id": oid, "name": name, "type": "company", "side": "work", "role": "owner"})
        toks = name_tokens(name)
        for c in d.chats:
            if c["chat_id"] not in chat_org and chat_mentions_org(c["name"], toks):
                if c["is_group"]:
                    chat_org[c["chat_id"]] = oid

    home_chats = [c for c in d.chats if c["is_group"] and c["chat_id"] not in chat_org
                  and matches(HOME_WORDS, c["name"]) and not matches(FAMILY_WORDS, c["name"])]
    if home_chats:
        orgs.append({"id": "home", "name": "Home", "type": "residence", "side": "home", "role": "owner"})
        for c in home_chats:
            chat_org[c["chat_id"]] = "home"
    farm_chats = [c for c in d.chats if c["is_group"] and c["chat_id"] not in chat_org and matches(FARM_WORDS, c["name"])]
    if farm_chats:
        orgs.append({"id": "farm", "name": "Farmhouse", "type": "vacation home", "side": "home", "role": "owner"})
        for c in farm_chats:
            chat_org[c["chat_id"]] = "farm"
    return orgs, chat_org


# ---------------------------------------------------------------- people

FAMILY_CIRCLES = {"spouse", "child", "parent", "in-law", "sibling", "relative"}
# Kinship tags people put in saved names. Not "uncle"/"aunty": used for any elder, family or not.
KIN_NAME_WORDS = r"\b(chacha|chachi|chachu|mama|mami|bua|phupha|mausi|masi|mausa|nana|nani|dada|dadi|bhabhi|jiju|jija|cousin)\b"


def _surnames(d, pid):
    return {n.split()[-1].lower() for n in d.names_of(pid) if len(n.split()) > 1 and len(n.split()[-1]) > 2}


def family_cues(d, result):
    """Family evidence a small model tends to miss, applied after it.

    - a surname shared with Arjun or with someone he placed as family means family;
    - a kinship tag in a saved name ("Meena Chacha") means family;
    - if nobody is his spouse yet, the 1:1 contact he messages far more than anyone (3x the next)
      who shares his surname is taken as the spouse.
    """
    fam_names = {d.me_name.split()[-1].lower()} if len(d.me_name.split()) > 1 else set()
    for p, c in d.seed["circles"].items():
        if c in FAMILY_CIRCLES:
            fam_names |= _surnames(d, p)
    for p, v in result.items():
        if p in d.seed["circles"] or v["circle"] in FAMILY_CIRCLES:
            continue
        if _surnames(d, p) & fam_names or matches(KIN_NAME_WORDS, " ".join(d.names_of(p)).lower()):
            result[p] = {"circle": "relative", "links": [], "looks_after": []}
    if any(v["circle"] == "spouse" for v in result.values()) or "spouse" in d.seed["circles"].values():
        return
    ranked = sorted(d.dm_chat, key=lambda p: -len(d.by_chat.get(d.dm_chat[p], [])))
    if len(ranked) >= 2:
        top, nxt = (len(d.by_chat.get(d.dm_chat[p], [])) for p in ranked[:2])
        me_sur = d.me_name.split()[-1].lower() if len(d.me_name.split()) > 1 else None
        if ranked[0] in result and ranked[0] not in d.seed["circles"] and top >= 3 * max(nxt, 1) \
                and me_sur in _surnames(d, ranked[0]):
            result[ranked[0]] = {"circle": "spouse", "links": [], "looks_after": []}


def heuristic_circle(d, pid, chat_org, orgs_by_id):
    """A best guess at someone's circle from names and the groups they sit in."""
    if pid in d.seed["circles"]:
        return d.seed["circles"][pid]
    names = " ".join(d.names_of(pid)).lower()
    surname = d.me_name.split()[-1].lower() if len(d.me_name.split()) > 1 else None
    if matches(STAFF_WORDS, names):
        return "staff"
    tokens = set()
    for o in orgs_by_id.values():
        tokens |= name_tokens(o["name"])
    if any(re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", names) for t in tokens):
        return "colleague"
    if matches(VENDOR_WORDS, names):
        return "vendor"
    if surname and surname in names.split():
        return "relative"
    groups = [d.chat_by_id[g] for g in d.groups_of.get(pid, [])]
    if any(matches(FAMILY_WORDS, g["name"]) for g in groups):
        return "relative"
    work_groups = [g for g in groups if g["chat_id"] in chat_org and orgs_by_id[chat_org[g["chat_id"]]]["side"] == "work"]
    if work_groups:
        # Someone in exactly one or two client-style groups is probably that client; someone across
        # many of the company's groups is probably on the team.
        if len(work_groups) >= 3:
            return "colleague"
        if any(re.search(r":|<>|\bx\b", g["name"].lower()) for g in work_groups):
            return "client"
        return "colleague"
    return "friend"
