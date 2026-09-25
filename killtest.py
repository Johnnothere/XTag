#!/usr/bin/env python3
"""
killtest.py — does XTag's subject matter appear in any ad archive at all?

This is the one question that decides whether ADINT is worth building into XTag,
and it is answerable in an afternoon. The join between paid and organic is the
canonical LANDING HOST. If the hosts XTag's corpora point at never appear in an
ad archive, the whole fusion is theoretical for this subject matter and you
should stop.

Run it, read the verdict, and believe the verdict.

    python3 killtest.py case1.json case2.json ...
    python3 killtest.py --url "https://xtag.up.railway.app/api/search?q=%23covid1948" --key $XTAG_API_KEY

An input can be:
  * an XTag /api/search payload saved as JSON
  * a case exported from the frontend
  * any JSON — the host extractor walks the whole structure looking for urls
"""

import json
import sys
import collections

import adint


def walk_hosts(obj, out, depth=0):
    """Find every URL-bearing field anywhere in an XTag payload.

    Deliberately structure-agnostic: the payload shape has changed before and
    a kill test that breaks on a schema change tells you nothing.
    """
    if depth > 12:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("url", "author_url", "link", "destination", "external_url") and isinstance(v, str):
                h = adint.canon_host(v)
                if h:
                    out[h] += 1
            else:
                walk_hosts(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            walk_hosts(v, out, depth + 1)


SOCIAL = {
    "x.com", "twitter.com", "t.co", "facebook.com", "instagram.com", "tiktok.com",
    "youtube.com", "youtu.be", "reddit.com", "t.me", "telegram.me", "linkedin.com",
    "threads.net", "bsky.app", "pinterest.com", "tumblr.com", "news.ycombinator.com",
}


def main(argv):
    # Local CSVs if present, otherwise download the archives (free, no auth).
    import os
    have_local = any(os.path.exists("snap%d.csv" % y) for y in (2024, 2025, 2026))
    ads, status = adint.load_snap(
        years=(2024, 2025, 2026),
        path_for=(lambda y: "snap%d.csv" % y) if have_local else None)
    print("ad archive: %s | %d records" % (status, len(ads)))
    ad_hosts = collections.Counter(h for a in ads for h in a["landing_hosts"])
    print("ad archive landing hosts: %d distinct\n" % len(ad_hosts))

    corpus = collections.Counter()
    for path in argv[1:]:
        try:
            with open(path) as f:
                walk_hosts(json.load(f), corpus)
        except Exception as e:
            print("  skipped %s (%s)" % (path, type(e).__name__))

    if not corpus:
        print("No hosts found in the inputs. Pass one or more XTag payload JSON files.")
        return 2

    # A post ON a platform is not a destination. Judge the join on real
    # destinations only, or every corpus 'overlaps' on youtube.com and the
    # test flatters itself.
    dest = collections.Counter({h: n for h, n in corpus.items() if h not in SOCIAL})
    overlap = {h: (n, ad_hosts[h]) for h, n in dest.items() if h in ad_hosts}

    print("corpus hosts (all):          %d" % len(corpus))
    print("corpus hosts (destinations): %d   <- the population that can join" % len(dest))
    print("hosts also in ad archive:    %d" % len(overlap))
    share = 100.0 * len(overlap) / max(1, len(dest))
    docs_covered = sum(n for n, _ in overlap.values())
    print("share of destination hosts:  %.1f%%" % share)
    print("documents behind them:       %d of %d\n" % (docs_covered, sum(dest.values())))

    if overlap:
        print("matched hosts (corpus docs / archive ads):")
        for h, (n, m) in sorted(overlap.items(), key=lambda kv: -kv[1][0])[:25]:
            print("   %-44s %5d / %5d" % (h, n, m))
        print()

    # Verdict. Stated before the run, not chosen after it.
    if len(overlap) == 0:
        print("VERDICT: NO OVERLAP.")
        print("  For this subject matter the paid<->organic join is theoretical.")
        print("  Do not build the fusion. Say so and stop. This is the honest outcome")
        print("  and it cost you an afternoon instead of a quarter.")
        return 1
    if share < 2.0:
        print("VERDICT: MARGINAL (<2%% of destination hosts).")
        print("  Enough to enrich a specific investigation, not enough to build a")
        print("  pipeline around. Treat ad data as a manual pivot, not a feature.")
        return 0
    print("VERDICT: JOIN IS REAL (>=2%% of destination hosts).")
    print("  Build the lag detector next — it is the part nobody has done, and")
    print("  watchlist_snapshots already holds the organic half.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
