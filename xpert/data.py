"""Loading the input folder and the derived views every stage shares."""
import collections
import datetime
import json
from pathlib import Path

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def fmt_ts(ts, with_day=True):
    d = datetime.datetime.fromtimestamp(ts, IST)
    return d.strftime("%a %d %b %H:%M") if with_day else d.strftime("%d %b %H:%M")


def iso_date(ts):
    return datetime.datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d")


class Data:
    def __init__(self, folder):
        folder = Path(folder)
        self.chats = read_jsonl(folder / "chats.jsonl")
        self.messages = sorted(read_jsonl(folder / "messages.jsonl"), key=lambda m: (m["ts"], m["id"]))
        seed_path = folder / "seed.json"
        self.seed = json.loads(seed_path.read_text(encoding="utf-8")) if seed_path.exists() else {}
        self.seed.setdefault("circles", {})
        self.seed.setdefault("organisations", [])
        self.seed.setdefault("goals", [])

        self.chat_by_id = {c["chat_id"]: c for c in self.chats}
        self.msg_by_id = {m["id"]: m for m in self.messages}
        self.by_chat = collections.defaultdict(list)
        for m in self.messages:
            self.by_chat[m["chat_id"]].append(m)

        self.people = sorted({p for c in self.chats for p in c["participants"]} - {"ME"})
        self.me_name = self._me_name()
        self.first_ts = self.messages[0]["ts"] if self.messages else 0
        self.last_ts = self.messages[-1]["ts"] if self.messages else 0

        # Per-person views
        self.dm_chat = {}  # person -> their 1:1 chat id
        self.groups_of = collections.defaultdict(list)  # person -> group chat ids
        for c in self.chats:
            others = [p for p in c["participants"] if p != "ME"]
            if not c["is_group"] and len(others) == 1:
                self.dm_chat[others[0]] = c["chat_id"]
            elif c["is_group"]:
                for p in others:
                    self.groups_of[p].append(c["chat_id"])
        self.sender_names = collections.defaultdict(collections.Counter)
        self.sent = collections.Counter()
        for m in self.messages:
            if m["sender_id"] != "ME":
                self.sender_names[m["sender_id"]][m["sender_name"]] += 1
                self.sent[m["sender_id"]] += 1
        self.me_count = collections.Counter(m["chat_id"] for m in self.messages if m["sender_id"] == "ME")

    def _me_name(self):
        names = collections.Counter(m["sender_name"] for m in self.messages if m["sender_id"] == "ME")
        return names.most_common(1)[0][0] if names else "the user"

    @property
    def me_first(self):
        return self.me_name.split()[0]

    def names_of(self, pid):
        """Every name this person goes by: their 1:1 chat's saved name, then names on their messages."""
        out = []
        if pid in self.dm_chat:
            n = self.chat_by_id[self.dm_chat[pid]]["name"].strip("‎ ").strip()
            if n:
                out.append(n)
        for n, _ in self.sender_names[pid].most_common(3):
            if n and n not in out:
                out.append(n)
        return out or [pid]

    def label(self, pid):
        return "ME" if pid == "ME" else f"{pid} {self.names_of(pid)[0]}"

    def chat_label(self, cid):
        c = self.chat_by_id[cid]
        name = c["name"].strip("‎ ").strip() or "(no name)"
        return name[:60]


def clip(text, n):
    text = (text or "").replace("\n", " / ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def msg_line(d, m, max_chars=350, people_ids=True):
    """One compact line per message, the format every prompt uses."""
    sender = "ME" if m["sender_id"] == "ME" else (m["sender_id"] if people_ids else d.names_of(m["sender_id"])[0])
    body = clip(m["body"], max_chars)
    if m["kind"] == "voice_transcript":
        body = "[voice] " + body
    elif m["kind"] == "document":
        body = "[doc] " + body
    return f"{m['id']} | {fmt_ts(m['ts'])} | {sender}: {body}"
