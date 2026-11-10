"""Sum usage.jsonl from a real run and price it.

    python3 dev_tools/usage_report.py <input_dir> <usd_per_M_input> <usd_per_M_output>

Prices are per million tokens; look them up for the model you use.
"""
import collections
import json
import sys

folder, pin, pout = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
n_msgs = sum(1 for line in open(f"{folder}/messages.jsonl", encoding="utf-8") if line.strip())
by = collections.defaultdict(lambda: [0, 0, 0])
for line in open("usage.jsonl", encoding="utf-8"):
    u = json.loads(line)
    t = (u.get("tag") or "").split(":")[0] or "?"
    by[t][0] += 1
    by[t][1] += u.get("prompt_tokens") or 0
    by[t][2] += u.get("completion_tokens") or 0
tot_in = sum(v[1] for v in by.values())
tot_out = sum(v[2] for v in by.values())
print(f"{'stage':<14}{'calls':>6}{'in':>10}{'out':>9}{'usd':>9}")
for t, (c, i, o) in sorted(by.items(), key=lambda kv: -kv[1][1]):
    print(f"{t:<14}{c:>6}{i:>10,}{o:>9,}{(i * pin + o * pout) / 1e6:>9.4f}")
usd = (tot_in * pin + tot_out * pout) / 1e6
print(f"{'total':<14}{sum(v[0] for v in by.values()):>6}{tot_in:>10,}{tot_out:>9,}{usd:>9.4f}")
print(f"${usd / n_msgs * 1000:.4f} per 1,000 messages ({n_msgs:,} messages)")
