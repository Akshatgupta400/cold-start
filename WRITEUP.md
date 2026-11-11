# Write-up

## What I focused on, and why

Open loops are 38 of the 74 data points (found 14, closed 14, help 10), so most of the effort went there. Circles (12) came
next: they also feed the loop prompts, since owner and help depend on who someone is. Organisations and chats (16) are mostly
one cheap call plus rules. Goals (8) get a conservative pass, because invented goals cost points and goals rarely surface in a
few weeks of chat.

Two facts about the scoring shaped the design:
- **Loops are scored only on the last two weeks.** The loop pass reads only the last 16 days, which cuts its cost by about
  60% on the six-week hidden set. Earlier weeks are still read for people, organisations and goals.
- **Organisations are matched by Jaccard over chats *and linked people*.** Linking every member of a 30-person ops group would
  swamp the match, so links go only to people he deals with directly (a 1:1 chat, seeded, or a small team group), plus staff.

## My approach

Rules where they are reliable, a model where judgment is needed, and a rule fallback for every stage, so `run.sh` always writes
a valid file. Four stages:

1. **Structure (1 call).** A compact directory of group chats (name, size, how much he posts, three sample lines) and the seeded
   organisation names. The model defines a short list of organisations and assigns each group. Client groups ("X: Nexus - Ad
   Targeting") go to his company, not to the client; family and friends' groups go to no organisation and are tied to a person.
   Every 1:1 chat is tied to the other person. The rules' organisations are then merged in: on dev the model returned only
   NEXUS and missed that "PMA ..." groups are PostMyAd, even with the seed naming it. Keeping rule organisations it missed, and
   their chats where it left them unassigned, took group chats from 26 to 34 right out of 40.
2. **People (≈1 call per 22 people).** An evidence card per person: saved names (which carry tags like "PMA", "Home", "sir"),
   their groups with each group's organisation, 12 lines spread across their 1:1 chat, a few of their group lines, a rule-based
   guess, and his surname and seeded people for calibration. People who never wrote and have no 1:1 chat keep the rule guess.
   Family cues the small model missed are applied after it: a surname shared with him or with seeded family, a kinship tag in a
   saved name ("Meena Chacha"), and a spouse rule (the 1:1 contact he messages over 3x more than anyone, sharing his surname).
3. **Loops.** Long chats are read in time order in chunks of up to 90 messages; loops still open after a chunk are carried into
   the next, so a later "paid" or "never mind" closes them. Short chats are packed several to a call. Busy groups where he never
   speaks are thinned to his name, birthdays and payment/deadline lines, and loops from those groups are kept only if they are
   clearly his (his message, his name, a birthday, a dated deadline); on dev this removed 29 loops of staff chasing each other
   about payment statuses without losing a real one. `done` needs a closing message and "ok" counts as agreement, not
   completion, because a false `done` costs a point. If a chat's model call fails, that chat gets the rule loops rather than
   none. A final batched call decides help for open loops, with statuses known, following the challenge's rules: stay out of
   other people's loops and the family's personal ones.
4. **Goals.** Goal signals come free from the loop pass; earlier weeks are filtered by a goal-word regex, sifted by the model in
   batches, and merged into at most 8 goals with evidence ids.

Testing without spending tokens: `oracle_test.py` swaps the model for the dev answer key and checks the plumbing. With perfect
answers the pipeline keeps **51.7 / 56**, so post-processing loses little; the gap is 1:1 chats the key ties to an organisation
and duplicate key entries (the organisation merge above costs 0.2 here and gains far more with a real model). `fuzz_test.py`
feeds 25 runs of malformed replies and every output stays valid.

| Dev set (56 points labelled) | Score |
|---|---|
| Naive starter | 5.4 |
| Rules only (`NO_LLM=1`) | 18.7 |
| Oracle (plumbing ceiling) | 51.7 |
| With `openai/gpt-oss-20b` (Groq) | **20.8** |

## Running it on a free tier

I ran everything on Groq's free tier (`openai/gpt-oss-20b`, settings in the README). Its limits shaped the client more than I
expected, and each was a silent failure first, because every stage falls back to rules and still writes a valid file:

- **403 on every call.** Groq's Cloudflare front rejects Python's default `urllib` User-Agent (error 1010); `curl` worked. The
  first "model" run was really a rules run. Fixed with a named User-Agent.
- **Empty replies.** gpt-oss is a reasoning model; its thinking counts against `max_tokens`, and the organisations call spent all
  6,000 on it and returned nothing. `LLM_REASONING_EFFORT` (low, medium for the one organisations call) fixed it.
- **413 on 8,000 tokens per minute.** Groq counts input plus `max_tokens`. The packs of small 1:1 chats, where most loops are,
  were all just over. The client now lowers `max_tokens` to fit and retries.
- **429 on 200,000 tokens per day.** Roughly one full dev run a day. The client honours short `retry-after` waits and fails fast
  on long ones (a daily quota), so the 25-minute budget is not spent sleeping. The on-disk cache let a run pick up the next day.

`openai/gpt-oss-120b` (its own quota) scored about the same as 20b on dev (17.8 against 17.9 before the later fixes), at twice
the price, so 20b stays.

## What it costs

Model: **`openai/gpt-oss-20b` on Groq** ($0.075 / $0.30 per million input / output tokens). On the dev set: **$0.0054 per
1,000 messages** ($0.024 for the 4,502 messages: 153,933 input and 42,600 output tokens, 42 calls, about 25 minutes).

How that was measured: the final run made the 28 loop, help and goal calls (108,049 in, 28,399 out, 20 minutes); the
organisations call and the 13 people calls came from the on-disk cache, so their tokens are taken from the earlier runs that
made them with the same prompts (45,884 in, 14,201 out, about 4 minutes). The time is the free tier's: one call at a time under
an 8,000 tokens-per-minute cap, which is close to the 25-minute budget. With parallel calls (`LLM_WORKERS`, default 6-8) on a
tier without that cap it should take a few minutes; I could not measure that.

Output is 22% of the tokens but 53% of the cost. Low reasoning effort keeps it there: at the default, reasoning alone used
6,000 output tokens on one call.

Measured input tokens on dev: loops and help ≈107k (70%), people ≈40k (26%), structure ≈5k. On the hidden set the
loop share falls, since only the last 16 of ~42 days are read, while people and goals grow slowly with the number of people and
earlier messages. Dev used 34k input tokens per 1,000 messages; I expect about 15k per 1,000 hidden messages (roughly the same
loop reading over 16 days, people and goals growing more slowly than the message count), so well under $0.005 per 1,000.

Where the money goes, and the levers: almost all of it is loop reading, input tokens. Each call repeats a ~900-token system prompt
kept byte-identical so providers that cache prompt prefixes can discount it; bodies are clipped to 350 characters; bot feeds and
silent busy groups are thinned before any call.

## Where it's wrong

Revised after the dev runs; each point gives what I measured on dev and what I expect on the hidden set.

- **Loop source message (the biggest loss).** A loop counts only if its source message is one the key accepts. On dev the
  model matched 25 of the key's 74 loops with 106 of its own (loops-found F1 0.28), against 19 for the rules alone and far
  below the 0.45-0.6 I first predicted. The causes: precision, as 81 of my loops are not in the key, 33 of them in the spouse chat,
  where I log follow-ups on a topic the key tracks once ("buy shares", "confirm payment for Vayu", "update on share purchase"),
  and 9 in one client group where staff are asked to change campaigns; repeated asks ("pls pay 2800" …
  "pls pay 3500 on this") where the key keeps several loops on one message and only one can match; and, before the client
  fixes, 1:1 chat packs lost to the free tier's limits. I expect F1 around 0.25-0.35 on the hidden set.
- **Status (`done` is mostly invisible).** None of the key's 49 `done` loops on dev cites a closing message: the labels come
  from Arjun's own records, so most were closed off WhatsApp. My loops stay `open` without a closing message, so most matched
  loops have the wrong status, nearly all in the safe direction (false `done` costs a point). Guessing from age and type
  (questions 13/14 `done`, old commitments 81%) gained at most 0.4 on dev and lost points at nearby thresholds, so I left it
  out. The hidden set is labelled by hand and may cite chat evidence more often; I expect 40-70% of matched loops to have the
  wrong status, with false `done` under 10%.
- **Help.** Not labelled on dev, so unmeasured. The hardest call is the spouse's requests: "order the Apple Pencil" (assist)
  against "need to discuss Dev" (leave). I expect the model to over-assist on family requests, giving a wrong `assist` on 10-20%
  of `leave` loops, the costly kind.
- **Circles.** On dev the model alone got 19 of 41 right, no better than the rules; the family cues bring it to 22 (54%). Its
  habit is to call anyone without an obvious cue a `friend`: 5 relatives with no shared surname, 3 colleagues, 3 clients, 2
  partners, a vendor and a staff member went that way. Silent group members get the rule guess. I expect 45-60% on the hidden
  set, against the 65-80% I first predicted.
- **Organisations and chats.** On dev I produce 3 organisations (PostMyAd, NEXUS, Home) against the key's 7: Nexus DaaS and
  Radiant are not recognised as his, and Office and Farmhouse have no group chat of their own. Group chats are 34 of 40 right.
  About 8% of chats are 1:1 chats the key ties to an organisation (an employee's chat tied to the company); I always tie 1:1
  chats to the person, so those are lost by design. On the hidden set I expect 2-4 organisations missing.
- **Goals.** Unlabelled on dev, so unmeasured. The final run produced the cap of 8. About 5 look real (an investment
  portfolio, a new car, building an AI team, the Nexus AI product, a home upgrade before Diwali) and about 3 are one-off tasks
  promoted to goals ("vacuum kids room", buying a fingerprint device). A match needs the same area and a shared evidence
  message; I expect 1-3 matches and 3-5 goals the key does not have, which cost precision.
- **Voice transcripts** (~50 in hidden, none in dev) are untested; they are read like text, marked `[voice]`.
