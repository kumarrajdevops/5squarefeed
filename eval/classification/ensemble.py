"""Ensemble of the 8 existing classification voters (offline, read-only, nothing is retuned).

Voters: A, B, C, C+abstain, C2, D, D+abstain, D2 -- built exactly as evaluate.py builds them.

Gate for threshold k (0..8), with c = voters saying cand, s = voters saying cand-or-rev ("surface"):
  c >= k            -> candidate lane
  else s >= k       -> review lane
  else              -> reject lane (silent)
k=8 needs unanimity; k=0 needs no agreement (everything is a candidate).

Usage (inside the api container, folder copied to /tmp/eval):
  python ensemble.py --labels labels_corrected.csv --embeddings emb_bge_small.npy
"""
import argparse
import os
from collections import Counter

import numpy as np

import evaluate as ev

VOTERS = ["A", "B", "C", "C+abstain", "C2", "D", "D+abstain", "D2"]


def build_voters(items, E, tau):
    bm = ev.b_matrix(items)
    v = {
        "A": [ev.approach_a(it) for it in items],
        "B": [ev.approach_b(it) for it in items],
        "C": ev.run_supervised(items, ev.tfidf_features(items), None),
        "C+abstain": ev.run_supervised(items, ev.tfidf_features(items), tau),
        "C2": ev.run_supervised(items, ev.tfidf_features(items, bm), tau),
        "D": ev.run_supervised(items, ev.embed_features(E), None),
        "D+abstain": ev.run_supervised(items, ev.embed_features(E), tau),
        "D2": ev.run_supervised(items, ev.embed_features(E, bm), tau),
    }
    return {k: [p["disp"] for p in v[k]] for k in VOTERS}


def gate(c, s, kc, ks):
    out = []
    for ci, si in zip(c, s):
        out.append("cand" if ci >= kc else ("rev" if si >= ks else "rej"))
    return out


def metrics(items, lanes):
    exp = [it["exp"] for it in items]
    n = len(items)
    cand = [i for i in range(n) if exp[i] == "cand"]
    rev = [i for i in range(n) if exp[i] == "rev"]
    rej = [i for i in range(n) if exp[i] == "rej"]
    return {
        "cand_in_cand_lane": sum(lanes[i] == "cand" for i in cand),
        "cand_surfaced": sum(lanes[i] != "rej" for i in cand),
        "silent_cand": sum(lanes[i] == "rej" for i in cand),
        "silent_rev": sum(lanes[i] == "rej" for i in rev),
        "rej_in_cand_lane": sum(lanes[i] == "cand" for i in rej),
        "rej_in_review": sum(lanes[i] == "rev" for i in rej),
        "cand_lane": lanes.count("cand"),
        "review_lane": lanes.count("rev"),
        "reject_lane": lanes.count("rej"),
        "n_cand": len(cand), "n_rev": len(rev), "n_rej": len(rej),
        "cand_lane_precision": sum(exp[i] == "cand" for i in range(n) if lanes[i] == "cand") / max(1, lanes.count("cand")),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default=os.path.join(ev.HERE, "labels_corrected.csv"))
    ap.add_argument("--embeddings", default=os.path.join(ev.HERE, "emb_bge_small.npy"))
    ap.add_argument("--tau", type=float, default=0.55)
    args = ap.parse_args()

    items = ev.load(args.labels)
    n = len(items)
    E = np.load(args.embeddings)
    assert E.shape[0] == n
    votes = build_voters(items, E, args.tau)
    print(f"labels: {args.labels} items: {n} expected: {dict(Counter(it['exp'] for it in items))}")

    print("\n== Individual voters (sanity check against earlier results) ==")
    print(f"{'voter':<11}{'cand->cand':>11}{'cand surf':>10}{'silent cand':>12}{'silent c|v':>11}{'rej->cand':>10}{'rej->rev':>9}{'review n':>9}{'review %':>9}")
    for name in VOTERS:
        m = metrics(items, votes[name])
        sl = m["silent_cand"] + m["silent_rev"]
        print(f"{name:<11}{m['cand_in_cand_lane']:>8}/{m['n_cand']:<2}{m['cand_surfaced']:>7}/{m['n_cand']:<2}{m['silent_cand']:>12}{sl:>11}"
              f"{m['rej_in_cand_lane']:>10}{m['rej_in_review']:>9}{m['review_lane']:>9}{100*m['review_lane']/n:>8.0f}%")

    c = [sum(votes[v][i] == "cand" for v in VOTERS) for i in range(n)]
    s = [sum(votes[v][i] != "rej" for v in VOTERS) for i in range(n)]

    print("\n== Vote distribution: number of voters saying 'cand' by expected label ==")
    for e in ("cand", "rev", "rej"):
        cnt = Counter(c[i] for i, it in enumerate(items) if it["exp"] == e)
        print(f"  expected {e}: " + " ".join(f"{k}:{cnt.get(k, 0)}" for k in range(9)))
    print("== Number of voters saying 'cand or rev' (surface) by expected label ==")
    for e in ("cand", "rev", "rej"):
        cnt = Counter(s[i] for i, it in enumerate(items) if it["exp"] == e)
        print(f"  expected {e}: " + " ".join(f"{k}:{cnt.get(k, 0)}" for k in range(9)))

    print("\n== Ensemble sweep: same k for the candidate lane and for surfacing ==")
    print("  cand lane: >=k voters say cand | review lane: >=k say cand-or-rev (and not cand lane) | else silent reject")
    print(f"{'k/8':<5}{'cand rec(auto)':>15}{'cand surfaced':>14}{'silent cand':>12}{'silent c|v':>11}{'rej->cand':>10}{'rej->rev':>9}"
          f"{'cand lane':>10}{'review':>8}{'review %':>9}{'reject lane':>12}{'cand prec':>10}")
    for k in range(8, -1, -1):
        m = metrics(items, gate(c, s, k, k))
        sl = m["silent_cand"] + m["silent_rev"]
        print(f"{k}/8  {m['cand_in_cand_lane']:>8}/{m['n_cand']:<2} ({100*m['cand_in_cand_lane']/m['n_cand']:>3.0f}%)"
              f"{m['cand_surfaced']:>8}/{m['n_cand']:<2}{m['silent_cand']:>7}{sl:>11}{m['rej_in_cand_lane']:>10}{m['rej_in_review']:>9}"
              f"{m['cand_lane']:>10}{m['review_lane']:>8}{100*m['review_lane']/n:>8.0f}%{m['reject_lane']:>12}{m['cand_lane_precision']:>10.2f}")

    print("\n== Exploratory: candidate threshold (rows) x surface threshold (cols); cell = silent_c|v / rej->cand / review% ==")
    print("   (in-sample choice of thresholds; use only to see the shape, not to claim accuracy)")
    print(f"{'kc \\ ks':<8}" + "".join(f"{ks:>16}" for ks in range(1, 9)))
    for kc in range(8, 0, -1):
        row = f"{kc:<8}"
        for ks in range(1, 9):
            m = metrics(items, gate(c, s, kc, ks))
            row += f"{m['silent_cand'] + m['silent_rev']:>5}/{m['rej_in_cand_lane']:>3}/{100*m['review_lane']/n:>3.0f}%  "
        print(row)

    print("\n== Per day, ensemble k=4 and k=5 (Oct 3-4 blind labels, Oct 5 not blind) ==")
    for k in (4, 5):
        lanes = gate(c, s, k, k)
        for day in sorted({it["date"] for it in items}):
            idx = [i for i, it in enumerate(items) if it["date"] == day]
            m = metrics([items[i] for i in idx], [lanes[i] for i in idx])
            print(f"  k={k} {day} n={len(idx)} cand_rec_auto={m['cand_in_cand_lane']}/{m['n_cand']} silent_c|v={m['silent_cand'] + m['silent_rev']} "
                  f"rej->cand={m['rej_in_cand_lane']} review={m['review_lane']} ({100*m['review_lane']/len(idx):.0f}%)")


if __name__ == "__main__":
    main()
