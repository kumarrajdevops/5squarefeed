"""Classification evaluation harness (offline, read-only, no production changes).

Approaches
  A  baseline keyword classifier (production app.filters.ai_relevance, unchanged)
  B  keyword + deterministic content-type rules (extended vocabulary, deal/tutorial/review/event
     patterns, development-verb cue). NOTE: rules were written after reading these same items, so B
     is optimistic; treat B's numbers as an upper bound for a hand-tuned rule set.
  C  supervised TF-IDF + logistic regression, leave-one-day-out CV  (C2 adds B's rule flags)
  D  local sentence embeddings + logistic regression, leave-one-day-out CV (needs embeddings.npy)

Usage (inside the api container, with this folder copied to /tmp/eval):
  python evaluate.py [--labels labels.csv] [--errors A|B|C|C2|D|D2] [--tau 0.55]
"""
import argparse
import csv
import os
import re
import sys
import warnings
from collections import Counter, defaultdict

import numpy as np

warnings.filterwarnings("ignore")
from scipy.sparse import csr_matrix, hstack
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.join(HERE, "..", ".."))
from app.filters.ai_relevance import AI_KEYWORDS, calculate_ai_relevance  # noqa: E402

DISPS = ["cand", "rev", "rej"]
NEWS_TYPES = {"dev", "rel", "res", "proj"}


def load(labels_path):
    items = list(csv.DictReader(open(os.path.join(HERE, "items.csv"), encoding="utf-8")))
    labels = {r["id"]: r for r in csv.DictReader(open(labels_path, encoding="utf-8"))}
    for it in items:
        lab = labels[it["id"]]
        it.update({k: lab[k] for k in ("tech", "ai", "ctype", "aud", "exp", "conf", "tags")})
        it["tagset"] = set(t for t in lab["tags"].split(",") if t)
        it["text"] = it["title"] + ". " + it["summary"]
    return items


# ----------------------------------------------------------------------------- A
def approach_a(it):
    rel, _score, _reason = calculate_ai_relevance(it["title"], it["summary"])
    ai = "core" if rel == "ai_candidate" else "none"
    return {"disp": "cand" if rel == "ai_candidate" else "rej", "ai": ai, "news": None}


# ----------------------------------------------------------------------------- B
CORE_EXTRA = [
    "chatgpt", "codex", "copilot", "apple intelligence", "genai", "gen ai", "model router", "openai",
    "qwen", "kimi", "grok", "perplexity", "muse", "inference engine", "fine-tuning", "fine-tuned",
    "open-weight", "open weight", "open weights", "agentic", "coding agent", "coding agents",
    "agent", "agents", "vibe-coded", "vibe coding", "chatbot", "chatbots", "machine intelligence",
    "ai-generated", "ai-powered", "superintelligence", "slop", "transformer", "embedding", "embeddings",
    "token", "tokens", "prompt", "prompts", "text-to-image", "diffusion model", "model weights",
    "siri", "alexa", "omni-model", "omni model",
]
ADJ_TERMS = [
    "robot", "robots", "robotaxi", "robotaxis", "humanoid", "drone", "drones", "autonomous", "self-driving",
    "data center", "data centers", "datacenter", "gpu", "gpus", "chip", "chips", "semiconductor", "tsmc",
    "surveillance", "smart glasses", "ray-ban", "open source", "open-source", "show hn", "github",
    "supercomputer", "facial recognition", "license plate", "quantum", "reinforcement learning",
]
DEAL_RE = re.compile(
    r"\b(deals?|discount(ed)?|price (drop|cut)|on sale|sale|lowest[- ]ever|all-time low|record-low|"
    r"\d+% off|\$\d+ off|£\d+ off|save up to|coupon|big deal days|prime day|black friday|cheaper than|"
    r"price hike|freebies?)\b", re.I)
STREAM_RE = re.compile(r"\b(free streams?|how to watch|live streams?|streams? online)\b", re.I)
EVENT_RE = re.compile(
    r"\b(register now|disrupt 2026|summit|webinar|lineup|judges|tickets?|conference pass|emtech|"
    r"what to expect during)\b", re.I)
REVIEW_RE = re.compile(
    r"\b(review|reviewed|i tested|i've tested|i test|hands-on|vs\.?|versus|which .* is right|"
    r"top \d|best .* (of|for)|\d+ best)\b", re.I)
TUT_RE = re.compile(
    r"^(how to|how do|how can|what are the|what is|are there|why is|here's why|here's how|"
    r"a (beginner|practical|complete)|guide to|\d+ (ways|tips|guidelines)|"
    r"computer vision:|tutorial)|\bhow to (build|use|turn|limit|disable|delete|install|set up|"
    r"fix|get|make)\b|\b(step-by-step|beginner)\b", re.I)
OPIN_RE = re.compile(
    r"\b(opinion|editorial|column|essay|op-ed|we need to|should|why (we|i|you)|the case for|"
    r"the guardian view)\b", re.I)
ROUNDUP_RE = re.compile(r"\b(podcast|newsletter|weekly|digest|roundup|this week|episode|on equity)\b", re.I)
PERSONAL_RE = re.compile(r"^(i |i've |my |we |our )", re.I)
DEV_RE = re.compile(
    r"\b(launch(es|ed)?|releas(es|ed)?|unveils?|announces?|introduc(es|ed)|adds?|adding|bans?|banned|"
    r"freez(es|ing)|froze|open[- ]sources?|ships?|rolls? out|raises?|acquires?|hires?|reports?|finds?|found|"
    r"sues?|arrest(ed|s)?|limit(s|ing)|halts?|cracks?|solves?|discovers?|detects?|tracking|tracks|"
    r"cuts?|kills?|blocks?|resigns?|rules?|ruled|testing|tests|new|beats?|outperforms?|"
    r"flagged|expands?|partners?|signs?|deploys?|deployed)\b", re.I)


def _terms_re(terms):
    return re.compile(r"\b(" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r")\b", re.I)


CORE_RE = _terms_re((AI_KEYWORDS - {"ai"}) | set(CORE_EXTRA))
BARE_AI_RE = re.compile(r"\bAI\b|\bA\.I\.\b")
ADJ_RE = _terms_re(ADJ_TERMS)


def b_flags(it):
    t, s = it["title"], it["summary"]
    core_t = bool(CORE_RE.search(t)) or bool(BARE_AI_RE.search(t))
    core_s = bool(CORE_RE.search(s)) or bool(BARE_AI_RE.search(s))
    adj = bool(ADJ_RE.search(t + " " + s))
    nonnews = None
    if DEAL_RE.search(t):
        nonnews = "deal"
    elif STREAM_RE.search(t):
        nonnews = "deal"
    elif EVENT_RE.search(t + " " + s):
        nonnews = "event"
    elif TUT_RE.search(t):
        nonnews = "tut"
    elif REVIEW_RE.search(t):
        nonnews = "rev"
    soft = None
    if OPIN_RE.search(t):
        soft = "opin"
    elif ROUNDUP_RE.search(t):
        soft = "round"
    elif PERSONAL_RE.search(t):
        soft = "opin"
    dev = bool(DEV_RE.search(t))
    return {"core_t": core_t, "core_s": core_s, "adj": adj, "nonnews": nonnews, "soft": soft, "dev": dev}


def approach_b(it):
    f = b_flags(it)
    ai = "core" if (f["core_t"] or f["core_s"]) else ("adj" if f["adj"] else "none")
    if f["nonnews"]:
        return {"disp": "rej", "ai": ai, "news": False}
    if f["soft"]:
        return {"disp": "rev" if ai != "none" else "rej", "ai": ai, "news": False}
    if f["core_t"]:
        return {"disp": "cand" if f["dev"] else "rev", "ai": ai, "news": f["dev"]}
    if f["core_s"] or f["adj"]:
        return {"disp": "rev", "ai": ai, "news": f["dev"]}
    return {"disp": "rej", "ai": ai, "news": False}


def b_matrix(items):
    cols = ["core_t", "core_s", "adj", "dev"]
    kinds = ["deal", "event", "tut", "rev"]
    soft = ["opin", "round"]
    rows = []
    for it in items:
        f = b_flags(it)
        rows.append([float(f[c]) for c in cols]
                    + [float(f["nonnews"] == k) for k in kinds]
                    + [float(f["soft"] == k) for k in soft])
    return csr_matrix(np.array(rows))


# ----------------------------------------------------------------------------- C / D
def lodo(items, make_features, target, tau=None, C=3.0):
    """Leave-one-day-out predictions of `target` (list of label values)."""
    days = sorted({it["date"] for it in items})
    preds = [None] * len(items)
    probs = [None] * len(items)
    for day in days:
        tr = [i for i, it in enumerate(items) if it["date"] != day]
        te = [i for i, it in enumerate(items) if it["date"] == day]
        Xtr, Xte = make_features(tr, te)
        ytr = [target[i] for i in tr]
        clf = LogisticRegression(max_iter=2000, C=C, class_weight="balanced")
        clf.fit(Xtr, ytr)
        P = clf.predict_proba(Xte)
        for k, i in enumerate(te):
            j = int(np.argmax(P[k]))
            preds[i] = clf.classes_[j]
            probs[i] = float(P[k][j])
    return preds, probs


def tfidf_features(items, extra=None):
    texts = [(it["title"] + " ") * 2 + it["summary"] for it in items]

    def make(tr, te):
        vec = TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True, stop_words="english")
        Xtr = vec.fit_transform([texts[i] for i in tr])
        Xte = vec.transform([texts[i] for i in te])
        if extra is not None:
            Xtr = hstack([Xtr, extra[tr]]).tocsr()
            Xte = hstack([Xte, extra[te]]).tocsr()
        return Xtr, Xte
    return make


def embed_features(E, extra=None):
    def make(tr, te):
        Xtr, Xte = csr_matrix(E[tr]), csr_matrix(E[te])
        if extra is not None:
            Xtr = hstack([Xtr, extra[tr]]).tocsr()
            Xte = hstack([Xte, extra[te]]).tocsr()
        return Xtr, Xte
    return make


def run_supervised(items, make, tau):
    out = {}
    for name, key in (("disp", "exp"), ("ai", "ai")):
        out[name], out[name + "_p"] = lodo(items, make, [it[key] for it in items])
    out["news"], _ = lodo(items, make, [it["ctype"] in NEWS_TYPES for it in items])
    out["aud"], _ = lodo(items, make, [it["aud"] != "l" for it in items])
    res = []
    for i in range(len(items)):
        d = out["disp"][i]
        if tau and out["disp_p"][i] < tau and d != "rev":
            d = "rev"
        res.append({"disp": d, "ai": out["ai"][i], "news": bool(out["news"][i]), "aud": bool(out["aud"][i])})
    return res


# ----------------------------------------------------------------------------- reporting
def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    f = 2 * p * r / (p + r) if p == p and r == r and p + r else float("nan")
    return p, r, f


def confusion(items, preds):
    M = {e: Counter() for e in DISPS}
    for it, p in zip(items, preds):
        M[it["exp"]][p["disp"]] += 1
    return M


def headline_metrics(items, preds):
    n = len(items)
    exp = [it["exp"] for it in items]
    pr = [p["disp"] for p in preds]
    cand = [i for i in range(n) if exp[i] == "cand"]
    nonrej = [i for i in range(n) if exp[i] != "rej"]
    rej = [i for i in range(n) if exp[i] == "rej"]
    hard_fn_cand = sum(pr[i] == "rej" for i in cand)
    hard_fn_any = sum(pr[i] == "rej" for i in nonrej)
    auto_fp = sum(pr[i] == "cand" for i in rej)
    rev_noise = sum(pr[i] == "rev" for i in rej)
    return {
        "n": n,
        "cand_recall_auto": sum(pr[i] == "cand" for i in cand) / len(cand),
        "cand_recall_surfaced": 1 - hard_fn_cand / len(cand),
        "nonrej_recall_surfaced": 1 - hard_fn_any / len(nonrej),
        "hard_FN_cand": hard_fn_cand,
        "hard_FN_cand_or_rev": hard_fn_any,
        "auto_FP": auto_fp,
        "rej_to_review": rev_noise,
        "review_load": sum(p == "rev" for p in pr) / n,
        "cand_precision": sum(exp[i] == "cand" for i in range(n) if pr[i] == "cand") / max(1, sum(p == "cand" for p in pr)),
        "surfaced_precision": sum(exp[i] != "rej" for i in range(n) if pr[i] != "rej") / max(1, sum(p != "rej" for p in pr)),
        "exact_acc": sum(e == p for e, p in zip(exp, pr)) / n,
    }


SLICES = [
    ("name_gap", lambda it: "name_gap" in it["tagset"]),
    ("implicit", lambda it: "implicit" in it["tagset"]),
    ("robot_emerging", lambda it: "robot_emerging" in it["tagset"]),
    ("oss_project", lambda it: "oss_project" in it["tagset"]),
    ("chips_infra", lambda it: "chips_infra" in it["tagset"]),
    ("policy_safety", lambda it: "policy_safety" in it["tagset"]),
    ("ai_nonnews (AI-labelled non-news)", lambda it: "ai_nonnews" in it["tagset"]),
    ("tutorials (ctype=tut)", lambda it: it["ctype"] == "tut"),
    ("deals/promos (deal,event,spons)", lambda it: it["ctype"] in {"deal", "event", "spons"}),
    ("opinions (opin)", lambda it: it["ctype"] == "opin"),
    ("reviews (rev)", lambda it: it["ctype"] == "rev"),
    ("roundups/background (round,bg)", lambda it: it["ctype"] in {"round", "bg"}),
    ("generic AI mention (AI core, aud=l)", lambda it: it["ai"] == "core" and it["aud"] == "l"),
    ("non-AI tech (nonai_tech)", lambda it: "nonai_tech" in it["tagset"]),
    ("thin summaries", lambda it: "thin" in it["tagset"]),
]


def slice_report(items, allpreds):
    names = list(allpreds)
    print("\n== Hard-example slices: per approach 'FN/FP/rev' ==")
    print("   FN = expected cand|rev but predicted rej (silently lost); FP = expected rej but predicted cand (auto-accepted noise);")
    print("   rev = how many in the slice were routed to human review")
    head = f"{'slice':<38}{'n':>4} {'exp c/v/r':>10}  " + "  ".join(f"{n:>11}" for n in names)
    print(head)
    for label, fn in SLICES:
        idx = [i for i, it in enumerate(items) if fn(it)]
        if not idx:
            continue
        ec = Counter(items[i]["exp"] for i in idx)
        cells = []
        for n in names:
            pr = [allpreds[n][i]["disp"] for i in idx]
            fnc = sum(1 for i in idx if items[i]["exp"] != "rej" and allpreds[n][i]["disp"] == "rej")
            fpc = sum(1 for i in idx if items[i]["exp"] == "rej" and allpreds[n][i]["disp"] == "cand")
            cells.append(f"{fnc}/{fpc}/{pr.count('rev')}".rjust(11))
        print(f"{label:<38}{len(idx):>4} {ec['cand']:>3}/{ec['rev']:>2}/{ec['rej']:>3}  " + "  ".join(cells))


def dimension_report(items, allpreds):
    print("\n== Four dimensions (accuracy / F1 of the dimension signal each approach exposes) ==")
    print("   topic = AI-relatedness core/adj/none (3-class acc) and tech-ish AI-vs-none F1 for ai!=none")
    print("   newsworthiness = ctype in {dev,rel,res,proj} vs not (F1 of the 'news' class)")
    print("   audience value = aud in {h,m} vs l (F1 of the 'valuable' class)")
    print(f"{'approach':<10}{'topic acc':>10}{'AI-vs-none F1':>15}{'news F1':>10}{'audience F1':>13}")
    for n, preds in allpreds.items():
        acc = sum(p["ai"] == it["ai"] for it, p in zip(items, preds)) / len(items)
        tp = sum(p["ai"] != "none" and it["ai"] != "none" for it, p in zip(items, preds))
        fp = sum(p["ai"] != "none" and it["ai"] == "none" for it, p in zip(items, preds))
        fn = sum(p["ai"] == "none" and it["ai"] != "none" for it, p in zip(items, preds))
        f_ai = prf(tp, fp, fn)[2]

        def f1_of(key, truth):
            if preds[0].get(key) is None:
                return None
            tp = sum(bool(p[key]) and truth(it) for it, p in zip(items, preds))
            fp = sum(bool(p[key]) and not truth(it) for it, p in zip(items, preds))
            fn = sum((not p[key]) and truth(it) for it, p in zip(items, preds))
            return prf(tp, fp, fn)[2]
        fn_ = f1_of("news", lambda it: it["ctype"] in NEWS_TYPES)
        fa_ = f1_of("aud", lambda it: it["aud"] != "l")
        fmt = lambda v: "   n/a" if v is None else f"{v:6.2f}"
        print(f"{n:<10}{acc:10.2f}{f_ai:15.2f}{fmt(fn_):>10}{fmt(fa_):>13}")


def by_day(items, allpreds):
    print("\n== Per day (Oct 3-4 labels blind to old classifier; Oct 5 not blind) ==")
    print(f"{'approach':<10}{'day':<12}{'n':>4}{'cand recall(auto)':>19}{'hard FN (c|v)':>15}{'auto FP':>9}{'review load':>13}")
    for n, preds in allpreds.items():
        for day in sorted({it["date"] for it in items}):
            idx = [i for i, it in enumerate(items) if it["date"] == day]
            m = headline_metrics([items[i] for i in idx], [preds[i] for i in idx])
            print(f"{n:<10}{day:<12}{m['n']:>4}{m['cand_recall_auto']:>19.2f}{m['hard_FN_cand_or_rev']:>15}{m['auto_FP']:>9}{m['review_load']:>13.2f}")


def print_errors(items, preds, name):
    print(f"\n== Errors for {name} ==")
    print("-- HARD FN (expected cand/rev, predicted rej) --")
    for it, p in zip(items, preds):
        if it["exp"] != "rej" and p["disp"] == "rej":
            print(f"  {it['id']} exp={it['exp']} tags={sorted(it['tagset'])} {it['title'][:95]}")
    print("-- AUTO FP (expected rej, predicted cand) --")
    for it, p in zip(items, preds):
        if it["exp"] == "rej" and p["disp"] == "cand":
            print(f"  {it['id']} ctype={it['ctype']} tags={sorted(it['tagset'])} {it['title'][:95]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default=os.path.join(HERE, "labels.csv"))
    ap.add_argument("--errors", action="append", default=[])
    ap.add_argument("--tau", type=float, default=0.55)
    ap.add_argument("--embeddings", default=os.path.join(HERE, "embeddings.npy"))
    args = ap.parse_args()

    items = load(args.labels)
    n = len(items)
    print(f"labels: {args.labels}  items: {n}  expected dist: {dict(Counter(i['exp'] for i in items))}")

    allpreds = {}
    allpreds["A"] = [approach_a(it) for it in items]
    allpreds["B"] = [approach_b(it) for it in items]
    bm = b_matrix(items)
    allpreds["C"] = run_supervised(items, tfidf_features(items), None)
    allpreds["C+abstain"] = run_supervised(items, tfidf_features(items), args.tau)
    allpreds["C2"] = run_supervised(items, tfidf_features(items, bm), args.tau)
    if os.path.exists(args.embeddings):
        E = np.load(args.embeddings)
        assert E.shape[0] == n, "embeddings row count must match items.csv"
        allpreds["D"] = run_supervised(items, embed_features(E), None)
        allpreds["D+abstain"] = run_supervised(items, embed_features(E), args.tau)
        allpreds["D2"] = run_supervised(items, embed_features(E, bm), args.tau)
    else:
        print("(no embeddings.npy: approach D not evaluated)")

    print("\n== Headline metrics ==")
    print("   cand recall(auto): expected-cand routed to cand | surfaced: cand or rev (not silently rejected)")
    print("   hard FN: expected cand|rev predicted rej | auto FP: expected rej predicted cand | rej->review: noise sent to the human")
    keys = ["cand_recall_auto", "cand_recall_surfaced", "nonrej_recall_surfaced", "hard_FN_cand", "hard_FN_cand_or_rev",
            "auto_FP", "rej_to_review", "review_load", "cand_precision", "surfaced_precision", "exact_acc"]
    print(f"{'metric':<26}" + "".join(f"{k:>12}" for k in allpreds))
    for k in keys:
        row = f"{k:<26}"
        for name, preds in allpreds.items():
            v = headline_metrics(items, preds)[k]
            row += f"{v:12.2f}" if isinstance(v, float) else f"{v:12d}"
        print(row)

    print("\n== Confusion matrices (rows=expected, cols=predicted cand/rev/rej) ==")
    for name, preds in allpreds.items():
        M = confusion(items, preds)
        print(f"{name:<10}" + "   ".join(f"{e}: " + "/".join(str(M[e][d]) for d in DISPS) for e in DISPS))

    dimension_report(items, allpreds)
    slice_report(items, allpreds)
    by_day(items, {k: v for k, v in allpreds.items() if k in ("A", "B", "C", "D")})
    for name in args.errors:
        print_errors(items, allpreds[name], name)


if __name__ == "__main__":
    main()
