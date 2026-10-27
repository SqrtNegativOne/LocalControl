"""Sanity-check the model's verdicts on canned activities (no enforcement, no screen access).

    python3 eval_judge.py [--model gemma3:4b]
"""

import argparse
import time

from localcontrol.config import Config
from localcontrol.controller import Controller
from localcontrol.macos import Activity

RULE = "For the next hour, I can only use this laptop to read papers or search for papers or technical material."

# (activity, expected allowed)
CASES = [
    (Activity("Firefox", url="https://arxiv.org/abs/2310.06825", tab_title="[2310.06825] Mistral 7B"), True),
    (Activity("Google Chrome", url="https://scholar.google.com/scholar?q=multi-agent+reinforcement+learning",
              tab_title="Google Scholar"), True),
    (Activity("Google Chrome", url="https://www.google.com/search?q=pytorch+scaled_dot_product_attention+backward",
              tab_title="pytorch scaled_dot_product_attention backward - Google Search"), True),
    (Activity("Safari", url="https://openreview.net/forum?id=abc123", tab_title="Attention Is All You Need | OpenReview"), True),
    (Activity("Firefox", url="https://docs.python.org/3/library/asyncio.html", tab_title="asyncio — Asynchronous I/O"), True),
    (Activity("Preview", window_title="vaswani2017attention.pdf"), True),
    (Activity("Firefox", url="https://x.com/home", tab_title="Home / X"), False),
    (Activity("Google Chrome", url="https://www.youtube.com/watch?v=xyz", tab_title="Top 10 NBA dunks of 2026 - YouTube"), False),
    (Activity("Google Chrome", url="https://www.youtube.com/watch?v=abc",
              tab_title="Stanford CS224N Lecture 8 - Self-Attention and Transformers - YouTube"), True),
    (Activity("Firefox", url="https://www.amazon.com/dp/B0C", tab_title="Amazon.com: Wireless Earbuds"), False),
    (Activity("Firefox", url="https://www.reddit.com/r/nba/", tab_title="r/nba"), False),
    (Activity("Google Chrome", url="https://www.google.com/search?q=celebrity+gossip",
              tab_title="celebrity gossip - Google Search"), False),
    (Activity("Steam", window_title="Steam"), False),
    (Activity("Messages", window_title="Messages"), False),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=Config.model)
    ap.add_argument("--rule", default=RULE)
    a = ap.parse_args()

    ctl = Controller(Config(model=a.model, vision="off", dry_run=True))
    # Don't touch the real daemon's saved rule or event log.
    ctl._save_policy = lambda: None
    ctl._log = lambda *args, **kw: None
    t0 = time.time()
    p = ctl.set_policy(a.rule)
    print(f"Parsed in {time.time() - t0:.1f}s: {p.rule!r}  ({'none' if p.expires_at is None else round((p.expires_at - p.started_at) / 60)} min)\n")

    correct = 0
    for act, want in CASES:
        t0 = time.time()
        v = ctl._ask_llm(p, act)
        ok = v.allowed == want
        correct += ok
        print(f"{'✓' if ok else '✗'} {'ALLOW' if v.allowed else 'BLOCK'} {v.scope:4s} {time.time() - t0:4.1f}s  {act.label[:70]}\n    {v.reason}")
    print(f"\n{correct}/{len(CASES)} as expected")


if __name__ == "__main__":
    main()
