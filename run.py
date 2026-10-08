"""
CLI for the Passenger Assistance Agent.

    python run.py                                  bluesky, 10 records
    python run.py --sources bluesky web --limit 15
    python run.py --sources all --limit 20
    python run.py --dry-run                        fetch only, no LLM calls

Writes a review queue to output/. Nothing is ever posted.
"""

import argparse
import json
import os
from collections import Counter
from datetime import datetime

import agent
import sources
import time

ALL = ["bluesky", "web", "hackernews", "appstore", "youtube"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", nargs="+", default=["bluesky"])
    ap.add_argument("--limit", type=int, default=10,
                    help="max records sent to the LLM")
    ap.add_argument("--out", default="output")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and prefilter only, no LLM calls")
    args = ap.parse_args()

    src = ALL if args.sources == ["all"] else args.sources
    os.makedirs(args.out, exist_ok=True)

    print("=" * 72)
    print("PASSENGER ASSISTANCE AGENT - UK rail")
    print("Drafts only. Nothing is posted. Every reply needs human review.")
    print("=" * 72)

    records = sources.fetch_all(src)
    kept = [r for r in records if agent.prefilter(r)[0]]
    print(f"\n{len(records)} fetched -> {len(kept)} passed the keyword gate "
          f"({len(records) - len(kept)} dropped before any LLM call)")

    if args.dry_run:
        for r in kept[:args.limit]:
            print(f"\n[{r['source']}] {r['text'][:160]}")
        return

    results, counts = [], Counter()
    batch = kept[:args.limit]
    for i, rec in enumerate(batch, 1):
        print(f"\n--- {i}/{len(batch)}  [{rec['source']}] @{rec['author'][:28]}")
        print(f"    {rec['text'][:140]}")
        res = agent.process(rec)
        for s in res["steps"]:
            print(f"    {s}")
        counts[res["action"]] += 1
        if res["draft"]:
            print(f"    >> {res['draft']['draft'][:220]}")
        results.append(res)
        time.sleep(4)   # free-tier Gemini is ~15 req/min; we make 2-3 per record

    print("\n" + "=" * 72)
    for action, n in counts.most_common():
        print(f"  {action:22} {n}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    md_path = os.path.join(args.out, f"review_queue_{stamp}.md")
    json_path = os.path.join(args.out, f"run_{stamp}.json")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Review queue - {stamp}\n\n")
        f.write("Drafts for human review. Nothing has been posted.\n\n")
        f.write(f"{len(batch)} posts assessed. "
                + ", ".join(f"{n} {a}" for a, n in counts.most_common())
                + "\n\n")

        esc = [r for r in results if r["action"] == "escalate"]
        if esc:
            f.write("## Escalated - needs a specialist, no draft written\n\n")
            for r in esc:
                rec, t = r["record"], r["triage"]
                f.write(f"- **{t['category']}/{t['severity']}** "
                        f"[@{rec['author']}]({rec['url']}) - "
                        f"{r['steps'][-1]}\n  > {rec['text'][:300]}\n\n")

        f.write("## Drafts\n\n")
        for r in results:
            if not r["draft"]:
                continue
            rec, t, d = r["record"], r["triage"], r["draft"]
            f.write(f"---\n\n### {rec['source']} / {t['category']} / "
                    f"{t['severity']}\n\n")
            f.write(f"**Original** @{rec['author']}, {rec['created_at'][:10]} "
                    f"- [link]({rec['url']})\n\n> {rec['text'][:600]}\n\n")
            f.write(f"**Draft reply**\n\n{d['draft']}\n\n")
            f.write(f"*Approach:* {d['rationale']}  \n"
                    f"*Triage:* {t['reason']}\n\n")
            if r["context"]:
                f.write("*Context used:* " + ", ".join(
                    f"[{c['title'][:40]}]({c['url']})" for c in r["context"]
                ) + "\n\n")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n  {md_path}")
    print(f"  {json_path}")
    agent.flush_traces()


if __name__ == "__main__":
    main()