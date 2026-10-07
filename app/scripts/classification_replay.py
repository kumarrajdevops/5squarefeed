# Read-only replay of the classification gate over stories already in the database.
#
# For each collection date it re-classifies every story from its title and summary with the
# CURRENT rules and compares the result with what is stored, then estimates what the change does
# to the candidate pool (source sufficiency -> Dedup -> ranking). It changes nothing: the session
# is a READ ONLY transaction that is rolled back, so ai_relevance, classifier fields,
# canonical_story_id, episodes, ranking and published state are never written.
#
#   python -m app.scripts.classification_replay                       # every classified date
#   python -m app.scripts.classification_replay --date 2026-10-06     # one date
#   python -m app.scripts.classification_replay --start-date 2026-10-03 --detail-date 2026-10-06
#
# "Old" is the stored ai_relevance. One reconstruction is applied: rows that the earlier
# rules-v2 pass flipped from ai_review to ai_candidate by hand are marked "(re-evaluated)" in
# filter_reason, and are treated here as their original ai_review state so the 2026-10-06
# baseline matches what the classifier produced before that pass.
#
# Reports land in --out-dir (default media/reports) as classification_replay_<stamp>.md/.json/.csv.
import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import text

from app.db import SessionLocal
from app.dedup.relations import effective_duplicate_story_ids
from app.filters.classification_rules import RULES_VERSION, classify
from app.filters.dedup import find_duplicate_match
from app.models import Episode, EpisodeStory, NewsItem, StoryState

LEGACY_REASON_CATEGORY = [
    ("AI term in title but no development verb", "development verb missing"),
    ("AI term only in the summary", "AI term only in the summary"),
    ("adjacent technology term only", "adjacent technology only"),
    ("opinion or personal-voice", "opinion / personal voice"),
]


def _read_only(db) -> None:
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SET TRANSACTION READ ONLY"))


def _cell(value, width: int = 80) -> str:
    value = (value or "").replace("|", "/").replace("\n", " ").strip()
    return value if len(value) <= width else value[: width - 1] + "..."


def old_state_of(state: StoryState) -> tuple[str, bool]:
    """(old ai_relevance, reconstructed?) -- see the module note on "(re-evaluated)" rows."""
    if state.ai_relevance == "ai_candidate" and (state.filter_reason or "").endswith("(re-evaluated)"):
        return "ai_review", True
    return state.ai_relevance, False


def legacy_category(reason: str | None) -> str:
    for needle, label in LEGACY_REASON_CATEGORY:
        if needle in (reason or ""):
            return label
    return "other"


def _legacy_reason(state: StoryState) -> str:
    if (state.filter_reason or "").endswith("(re-evaluated)"):
        # Only a missing development verb could have been fixed by that pass.
        return "Review: AI term in title but no development verb"
    return state.filter_reason


def _bucket(old: str, new: str) -> str:
    return f"{old} -> {new}"


class SimItem:
    def __init__(self, item: NewsItem):
        self.id, self.title, self.published_at = item.id, item.title, item.published_at


def simulate_pool(day_rows, new_candidate_ids, pinned: set[int], historical_dups: set[int], earlier_primary: set[int]) -> dict:
    """Estimated candidate pool for one day if `new_candidate_ids` were the ai_candidates."""
    cands = [(item, state) for item, state in day_rows if item.id in new_candidate_ids]
    after_sufficiency = [(i, s) for i, s in cands if s.source_sufficiency != "insufficient"]

    # Same-day title Dedup exactly as app/tasks/dedup.py would run it (oldest first, pinned first).
    ordered = sorted(after_sufficiency, key=lambda r: (r[0].published_at is None, r[0].published_at))
    ordered = [r for r in ordered if r[0].id in pinned] + [r for r in ordered if r[0].id not in pinned]
    canonical: list[SimItem] = []
    duplicates = 0
    survivors = []
    for item, state in ordered:
        if item.id in pinned:
            canonical.append(SimItem(item))
            survivors.append(item.id)
            continue
        match, _ = find_duplicate_match(item.title, item.published_at, item.id, canonical)
        if match is not None:
            duplicates += 1
        else:
            canonical.append(SimItem(item))
            survivors.append(item.id)
    after_dedup = [i for i in survivors if i not in historical_dups]
    reaching_ranking = [i for i in after_dedup if i not in earlier_primary]
    return {
        "candidates": len(cands),
        "after_source_sufficiency": len(after_sufficiency),
        "after_title_dedup": len(survivors),
        "after_historical_dedup": len(after_dedup),
        "reaching_ranking": len(reaching_ranking),
    }


def run(args) -> dict:
    db = SessionLocal()
    _read_only(db)
    try:
        rows = (
            db.query(NewsItem, StoryState)
            .join(StoryState, StoryState.id == NewsItem.id)
            .order_by(NewsItem.collection_date, NewsItem.id)
            .all()
        )
        by_date: dict[date, list] = defaultdict(list)
        for item, state in rows:
            by_date[item.collection_date].append((item, state))
        dates = sorted(by_date)
        if args.date:
            dates = [date.fromisoformat(args.date)]
        if args.start_date:
            dates = [d for d in dates if d >= date.fromisoformat(args.start_date)]
        if args.end_date:
            dates = [d for d in dates if d <= date.fromisoformat(args.end_date)]

        episodes = {e.id: e for e in db.query(Episode).all()}
        selected_on: dict[date, set[int]] = defaultdict(set)
        primary_date: dict[int, date] = {}
        for sid, eid, status in db.query(EpisodeStory.story_id, EpisodeStory.episode_id, EpisodeStory.selection_status).all():
            ep_date = episodes[eid].episode_date
            if status == "primary":
                selected_on[ep_date].add(sid)
                primary_date.setdefault(sid, ep_date)
        historical_dups = effective_duplicate_story_ids(db)

        per_date, changes, review_breakdown = {}, [], defaultdict(Counter)
        for d in dates:
            day = by_date[d]
            old_counts, new_counts, transitions = Counter(), Counter(), Counter()
            old_candidate_ids, new_candidate_ids = set(), set()
            for item, state in day:
                old, reconstructed = old_state_of(state)
                verdict = classify(item.title, item.raw_summary)
                new = "ai_candidate" if verdict.disposition == "candidate" else "not_ai"
                old_counts[old] += 1
                new_counts[new] += 1
                transitions[_bucket(old, new)] += 1
                if old == "ai_candidate":
                    old_candidate_ids.add(item.id)
                if new == "ai_candidate":
                    new_candidate_ids.add(item.id)
                legacy_reason = None
                if old == "ai_review":
                    legacy_reason = _legacy_reason(state)
                    review_breakdown[d][(legacy_category(legacy_reason), new)] += 1
                if old != new:
                    changes.append({
                        "story_id": item.id, "date": d.isoformat(), "title": item.title,
                        "old": old, "new": new, "old_version": state.classifier_version or "keyword",
                        "old_reconstructed": reconstructed,
                        "old_reason": legacy_reason or state.filter_reason,
                        "new_reason": verdict.reason, "ai_relatedness": verdict.ai_relatedness,
                        "content_type": verdict.content_flag or verdict.development or "-",
                        "legacy_category": legacy_category(legacy_reason) if old == "ai_review" else "",
                    })
            collected = len(day)
            pinned = set(selected_on.get(d, ()))
            earlier = {sid for sid, pd in primary_date.items() if pd < d}
            old_pool = simulate_pool(day, old_candidate_ids, pinned, historical_dups, earlier)
            new_pool = simulate_pool(day, new_candidate_ids, pinned, historical_dups, earlier)
            per_date[d] = {
                "date": d.isoformat(), "collected": collected,
                "old": dict(old_counts), "new": dict(new_counts), "transitions": dict(transitions),
                "old_candidate_rate": round(len(old_candidate_ids) / collected, 3) if collected else 0,
                "new_candidate_rate": round(len(new_candidate_ids) / collected, 3) if collected else 0,
                "old_pool": old_pool, "new_pool": new_pool,
                "selected_in_stored_episode": len(pinned),
                "new_pool_can_fill_25": new_pool["reaching_ranking"] + len(pinned & new_candidate_ids) >= 25,
            }
        db.rollback()
    finally:
        db.close()
    return {"rules_version": RULES_VERSION, "dates": per_date, "changes": changes, "review_breakdown": review_breakdown}


def _dates_table(result) -> list[str]:
    out = ["| Date | Collected | ai_candidate old -> new | ai_review old -> new | not_ai old -> new | "
           "Candidate rate old -> new | Reach Dedup old -> new | Reach ranking old -> new |",
           "|---|---:|---|---|---|---|---|---|"]
    for d, r in result["dates"].items():
        o, n = r["old"], r["new"]
        out.append(
            f"| {d.isoformat()} | {r['collected']} | {o.get('ai_candidate', 0)} -> {n.get('ai_candidate', 0)} | "
            f"{o.get('ai_review', 0)} -> {n.get('ai_review', 0)} | {o.get('not_ai', 0)} -> {n.get('not_ai', 0)} | "
            f"{r['old_candidate_rate']:.1%} -> {r['new_candidate_rate']:.1%} | "
            f"{r['old_pool']['after_source_sufficiency']} -> {r['new_pool']['after_source_sufficiency']} | "
            f"{r['old_pool']['reaching_ranking']} -> {r['new_pool']['reaching_ranking']} |")
    return out


def write_reports(result: dict, out_dir: Path, detail_date: date | None) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base = out_dir / f"classification_replay_{stamp}"
    changes = result["changes"]

    csv_path = base.with_suffix(".csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        cols = ["story_id", "date", "title", "old", "new", "old_version", "old_reconstructed", "old_reason",
                "new_reason", "ai_relatedness", "content_type", "legacy_category"]
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(changes)

    json_path = base.with_suffix(".json")
    payload = {
        "rules_version": result["rules_version"],
        "dates": {d.isoformat(): r for d, r in result["dates"].items()},
        "review_breakdown": {
            d.isoformat(): {f"{cat} -> {new}": n for (cat, new), n in c.items()}
            for d, c in result["review_breakdown"].items()
        },
        "changes": changes,
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    md = [f"# Classification replay ({result['rules_version']})", "",
          "Read-only: one READ ONLY transaction, rolled back. Nothing was written to the database.", "",
          "## Per-date before / after", ""] + _dates_table(result)

    md += ["", "## Pool impact (new classifier, estimated)", "",
           "Source sufficiency is the stored value (NULL = not assessed = passes, as ranking treats it). "
           "Title Dedup is simulated with the production matcher; historical Dedup uses the stored decisions only, "
           "so it undercounts for stories that have never been through that stage.", "",
           "| Date | Candidates | After source sufficiency | After title Dedup | After historical Dedup | Reaching ranking | Selected (stored episode) |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for d, r in result["dates"].items():
        p = r["new_pool"]
        md.append(f"| {d.isoformat()} | {p['candidates']} | {p['after_source_sufficiency']} | {p['after_title_dedup']} | "
                  f"{p['after_historical_dedup']} | {p['reaching_ranking']} | {r['selected_in_stored_episode']} |")

    for d, r in result["dates"].items():
        if detail_date and d != detail_date:
            continue
        md += ["", f"## {d.isoformat()} detail", "", "Transitions: " + ", ".join(
            f"{k}: {v}" for k, v in sorted(r["transitions"].items())), ""]
        bd = result["review_breakdown"].get(d)
        if bd:
            md += ["### Old ai_review stories by cause", "",
                   "| Cause | Old count | -> ai_candidate | -> not_ai |", "|---|---:|---:|---:|"]
            for cat in [c for _, c in LEGACY_REASON_CATEGORY] + ["other"]:
                cand, rej = bd.get((cat, "ai_candidate"), 0), bd.get((cat, "not_ai"), 0)
                if cand + rej:
                    md.append(f"| {cat} | {cand + rej} | {cand} | {rej} |")
            md.append("")
        md += ["### Story-level changes", "",
               "| Story | Date | Title | Old | New | Reason | AI relatedness | Content / development |",
               "|---:|---|---|---|---|---|---|---|"]
        order = {"ai_review -> ai_candidate": 0, "ai_review -> not_ai": 1, "not_ai -> ai_candidate": 2, "ai_candidate -> not_ai": 3}
        day_changes = sorted((c for c in changes if c["date"] == d.isoformat()),
                             key=lambda c: (order.get(f"{c['old']} -> {c['new']}", 9), c["story_id"]))
        for c in day_changes:
            md.append(f"| {c['story_id']} | {c['date']} | {_cell(c['title'])} | {c['old']} | {c['new']} | "
                      f"{_cell(c['new_reason'], 70)} | {c['ai_relatedness']} | {c['content_type']} |")

    md_path = base.with_suffix(".md")
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")
    return [md_path, json_path, csv_path]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--detail-date", default="2026-10-06", help="date whose story-level table is written")
    parser.add_argument("--out-dir", default="media/reports")
    args = parser.parse_args()
    result = run(args)
    paths = write_reports(result, Path(args.out_dir), date.fromisoformat(args.detail_date) if args.detail_date else None)
    for r in result["dates"].values():
        print(r["date"], "collected", r["collected"], "old", r["old"], "new", r["new"],
              "pool", r["new_pool"]["reaching_ranking"])
    for p in paths:
        print("wrote", p)


if __name__ == "__main__":
    main()
