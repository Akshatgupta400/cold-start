# Project notes: what was done, and why

This file explains the project from scratch: what it is, what was changed, what went wrong along the way, and where it stands.
For how to run it, see [README.md](README.md). For the formal write-up that is scored, see [WRITEUP.md](WRITEUP.md).

## 1. What this project is

The task (the "Cold Start" challenge) gives a program a few weeks of one person's WhatsApp messages. The person here is called
Arjun. The program has to work out, as a new personal assistant would on day one:

- **People:** each contact's relationship to him, called a "circle" (spouse, colleague, client, vendor, friend and so on).
- **Organisations:** the companies and households he runs or belongs to, and which group chats belong to which.
- **Open loops:** things still in motion, such as promises he made, things he is waiting on, deadlines and questions to answer,
  and whether each is still open or already done.
- **Help:** for each open loop, whether the assistant should help, remind him, or stay out of it.
- **Goals:** what he is working towards over weeks or months.

It writes all of this to one JSON file. A checker script (`check.py`) compares the file with an answer key and gives a score
out of 56. The organisers also score cost (12 points), the write-up (7) and whether it runs with one command (7).

The program combines simple rules with calls to a large language model (LLM). Every stage has a rule-only fallback, so it
always produces a valid file even when the model fails.

## 2. The starting point

The code was already written when this work began. What was left before submitting:

1. Choose a model, get an API key and run the program on the practice data (`data/dev`, 4,502 messages).
2. Score the result and read the loops it found, since that is where tuning matters most.
3. Fill in the blanks in `WRITEUP.md` (score, cost, tokens) and revise its predictions of where it goes wrong.
4. Save the AI chat used for the work into `ai-logs/`.

## 3. The choice of model

The model is **`openai/gpt-oss-20b`**, served by **Groq**, on Groq's **free tier**. Paying for a model was not an option, and
Groq offers a free tier with an OpenAI-compatible API, which the code already supports. No code change was needed to use it,
only three settings:

```bash
export LLM_BASE_URL=https://api.groq.com/openai/v1
export LLM_API_KEY=<your Groq key>
export LLM_MODEL=openai/gpt-oss-20b
```

The free tier has hard limits, and most of the work below came from them:

| Limit | Value | What it meant |
|---|---|---|
| Tokens per day | 200,000 | About one full run of the practice data per day |
| Tokens per minute | 8,000, counting the reply budget | Large requests are rejected outright |
| Requests per day | 1,000 | Never a problem |

## 4. What went wrong, and how each problem was fixed

Each problem below was **silent** at first: the program fell back to rules and still wrote a valid file, so the score looked
like the model was simply weak. Finding each one meant reading the logs closely.

| # | Symptom | Real cause | Fix |
|---|---|---|---|
| 1 | All 49 model calls failed in 11 seconds with error 403 | Groq's firewall blocks Python's default web client name. The same key worked with `curl`. | Send a named client ("User-Agent") with each call (`llm.py`) |
| 2 | Some replies came back empty | The model "thinks" before answering, and its thinking used up the whole reply budget | A setting for how much the model reasons: low for most calls, medium for the one organisations call (`LLM_REASONING_EFFORT`) |
| 3 | Many calls rejected with error 413 | Groq counts the input **plus the maximum reply length** against the 8,000-per-minute limit. Most rejected calls were only slightly over. | When this happens, shrink the maximum reply length to fit and retry (`llm.py`) |
| 4 | Runs spent most of their time waiting, then stopped | Error 429 was the **daily** quota. The code kept retrying for minutes, which used up the 25-minute time limit. | Wait only for short delays; give up at once on a daily quota, so the rest of the run continues (`llm.py`) |
| 5 | Replies that had failed were reused on the next run | The on-disk cache saved empty replies | Only save replies with content (`llm.py`) |
| 6 | Updates closing a loop were ignored | The model named loops by message id, but the code only understood its own labels such as `L1` | Accept both (`xpert/loops.py`) |
| 7 | One failed call meant no loops at all for those chats | No fallback at the level of a single chat | Those chats get the rule-based loops instead (`xpert/loops.py`) |

## 5. Changes that improved the results

Each change was measured against the answer key before it was kept, and checked so it does not break the plumbing (see
section 7).

1. **Organisations.** The model kept returning only one company (NEXUS) and missed that groups named "PMA …" belong to the
   other company, PostMyAd. The fix keeps the organisations the rules find and merges them with the model's answer. Group
   chats assigned correctly rose from 26 to 34 out of 40. (`xpert/structure.py`)
2. **Noise from busy work groups.** In a 20-person operations group, staff ask each other to update payment records. The model
   logged 29 of these as Arjun's loops. Loops from busy groups he rarely speaks in are now kept only if clearly his: his own
   message, his name, a birthday, or a dated deadline. No real loop was lost. (`xpert/loops.py`)
3. **Family relationships.** The small model labels almost anyone a "friend". Three rules now run after it:
   - a surname shared with Arjun, or with someone he already marked as family, means family;
   - a kinship word in a saved contact name (for example "Meena Chacha", meaning uncle) means family;
   - the one-to-one contact he messages far more than anyone else (at least 3 times the next), who shares his surname, is taken
     to be his spouse.

   (`xpert/rules.py`, `xpert/people.py`)
4. **Other small fixes:** a bug in a test script when the folder name has a space (`dev_tools/oracle_test.py`), run logs kept
   out of git (`.gitignore`), and the new settings documented in `README.md`.

## 6. Results

| Version | Score (out of 56) |
|---|---|
| Every model call failing (before fix 7, failed chats got no loops at all) | 13.6 |
| Rules only, no model (`NO_LLM=1`), after the rule improvements above | 18.7 |
| First run where the model actually ran (most calls blocked) | 17.0 |
| **Final run, every model call succeeded** | **20.8** |
| Perfect answers fed in (the most the code can score) | 51.7 |

Final score by part:

| Part | Score |
|---|---|
| Circles (people's relationships) | 6.4 / 12 |
| Organisations | 1.7 / 8 |
| Chats (which chat belongs to what) | 6.7 / 8 |
| Open loops found | 3.9 / 14 |
| Open loops with the right status | 2.1 / 14 |

**Cost:** about **$0.0054 per 1,000 messages**, or $0.024 for the whole practice set (42 model calls, about 196,000 tokens).
That is very cheap, which helps the separate cost score.

**A bigger model did not help.** `openai/gpt-oss-120b`, more than five times the size, scored about the same (17.8 against
17.9 at the time) for twice the price. So the main limits were the free tier's restrictions and the data, not the model's size.

## 7. How the changes were checked

- `dev_tools/oracle_test.py` replaces the model with the answer key. It shows whether the code around the model loses
  anything; it scores 51.7 of 56.
- `dev_tools/fuzz_test.py` feeds 25 runs of broken model replies. Every output must still be valid, and every one was.
- `check.py` scores a real run against the answer key.
- Every change was rerun through all three before it was kept.

## 8. Known limits

- **Finished loops are mostly invisible.** The answer key marks 49 loops as done, but none of them points to a message showing
  it was done. Most were finished outside WhatsApp, in person or by phone. A program reading only the chat cannot know, so it
  leaves them open. Guessing from age and type was tested and dropped: it gained at most 0.4 points and lost points with slightly
  different settings.
- **Too many loops in the spouse chat.** Of the 106 loops found, 81 are not in the answer key; 33 of those are follow-ups in
  the spouse chat on a topic the key tracks only once.
- **Relationships remain weak** (about 54% right). The small model defaults to "friend" for clients, colleagues and distant
  relatives.
- **Organisations without group chats** (an office, a farmhouse) are not found.
- **Run time:** on the free tier, one call at a time, a full run takes about 25 minutes, close to the time limit. A paid tier
  with parallel calls should take a few minutes; this was not measured.

## 9. Where things are

| What | Where |
|---|---|
| Code, write-up and logs | This repository (private): https://github.com/Akshatgupta400/cold-start |
| How to run | [README.md](README.md) |
| Scored write-up | [WRITEUP.md](WRITEUP.md) |
| Full AI session log (API key redacted) | [ai-logs/claude-code-session.md](ai-logs/claude-code-session.md) |
| Practice data | Not in the repository, by the challenge's rules |

## 10. Possible next step

One experiment is left: changing the loops instruction to stop logging repeated follow-ups on the same topic. It might gain
1 to 2 points, or lose some. It needs a day's free quota to test. The current version is complete and ready to submit
without it.
