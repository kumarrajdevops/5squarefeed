# Read-only replay of the historical duplicate detector over stories already in the database.
#
# For each collection date it rebuilds what the previous Dedup (TF-IDF repeat check against past
# primaries) would have decided, runs the semantic detector as of that date, and writes a
# comparison report. It changes nothing: the session is a READ ONLY transaction that is rolled
# back, nothing is written to editorial/raw tables, and the only artifact outside the report
# files is the on-disk embedding vector cache (under media/.model_cache, not a database).
#
#   python -m app.scripts.dedup_replay                          # every date
#   python -m app.scripts.dedup_replay --date 2026-10-02        # one date
#   python -m app.scripts.dedup_replay --limit 40               # evenly spaced sample of 40 stories
#   python -m app.scripts.dedup_replay --corpus-mode approved+draft
#   python -m app.scripts.dedup_replay --max-story-id 796       # leave out fixture rows above an id
#
# Reports land in --out-dir (default media/reports) as dedup_replay_<stamp>.md and .json.
import argparse
import json
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import func, text

from app.db import SessionLocal
from app.dedup.decision import METHOD_VERSION
from app.dedup.detector import HistoricalCorpus, Stats, detect_stories, load_features, published_primary_rows
from app.dedup.embedder import default_embedder
from app.filters.content_similarity import (
    CONTENT_SIMILARITY_THRESHOLD,
    compute_cross_corpus_similarity,
    get_comparable_text,
)
from app.models import Episode, EpisodeStory, NewsItem, StoryState

SAME_SUBJECT_NEW_RULES = {
    "new_development_type", "different_development", "incremental_new_content",
    "launch_new_numbers", "development_new_numbers", "insufficient_overlap",
}
STRONG_MISS_SIM = 0.85


def _title(text_: str, width: int = 72) -> str:
    text_ = (text_ or "").replace("|", "/").replace("\n", " ").strip()
    return text_ if len(text_) <= width else text_[: width - 1] + "..."


def _read_only(db) -> None:
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SET TRANSACTION READ ONLY"))


def baseline(db, max_id: int | None) -> dict:
    def n(query):
        return int(query.scalar() or 0)

    items = db.query(func.count(NewsItem.id))
    states = db.query(func.count(StoryState.id)).join(NewsItem, NewsItem.id == StoryState.id)
    if max_id:
        items = items.filter(NewsItem.id <= max_id)
        states = states.filter(NewsItem.id <= max_id)
    ep_stories = (
        db.query(func.count(EpisodeStory.id))
        .join(Episode, Episode.id == EpisodeStory.episode_id)
        .filter(EpisodeStory.selection_status == "primary")
    )
    published = ep_stories.filter((Episode.status == "approved") | (Episode.publish_status == "published"))
    if max_id:
        ep_stories = ep_stories.filter(EpisodeStory.story_id <= max_id)
        published = published.filter(EpisodeStory.story_id <= max_id)
    return {
        "stories": n(items),
        "ai_candidates": n(states.filter(StoryState.ai_relevance == "ai_candidate")),
        "same_day_duplicates": n(
            states.filter(StoryState.ai_relevance == "ai_candidate", StoryState.canonical_story_id.isnot(None))
        ),
        "stored_historical_repeat_flags": n(states.filter(StoryState.repeats_story_id.isnot(None))),
        "episodes": n(db.query(func.count(Episode.id))),
        "primaries_all_episodes": n(ep_stories),
        "primaries_approved_or_published": n(published),
    }


def run(args) -> dict:
    wall = time.perf_counter()
    db = SessionLocal()
    _read_only(db)
    max_id = args.max_story_id

    base = baseline(db, max_id)

    # ---- stories and dates ----------------------------------------------------------------
    q = db.query(NewsItem.id, NewsItem.collection_date, NewsItem.title, NewsItem.raw_content,
                 NewsItem.raw_summary, StoryState.ai_relevance, StoryState.canonical_story_id,
                 StoryState.source_sufficiency, StoryState.repeats_story_id).join(
        StoryState, StoryState.id == NewsItem.id, isouter=True)
    if max_id:
        q = q.filter(NewsItem.id <= max_id)
    rows = q.all()
    by_date: dict[date, list] = defaultdict(list)
    for r in rows:
        by_date[r.collection_date].append(r)
    dates = sorted(by_date)
    if args.date:
        dates = [date.fromisoformat(args.date)]
    if args.start_date:
        dates = [d for d in dates if d >= date.fromisoformat(args.start_date)]
    if args.end_date:
        dates = [d for d in dates if d <= date.fromisoformat(args.end_date)]

    # ---- episodes -------------------------------------------------------------------------
    episodes = {e.id: e for e in db.query(Episode).all()}
    primary_rows = db.query(EpisodeStory.story_id, EpisodeStory.episode_id, EpisodeStory.selection_status).all()
    primary_episode_date: dict[int, date] = {}  # story -> date of the episode it was primary in
    selected_on: dict[date, set[int]] = defaultdict(set)
    backup_on: dict[date, set[int]] = defaultdict(set)
    for sid, eid, status in primary_rows:
        if max_id and sid > max_id:
            continue
        ep_date = episodes[eid].episode_date
        if status == "primary":
            primary_episode_date.setdefault(sid, ep_date)
            selected_on[ep_date].add(sid)
        else:
            backup_on[ep_date].add(sid)

    # ---- candidate sets as of each date, and the old (TF-IDF) result ----------------------
    candidates: dict[int, dict] = {}
    date_rows: dict[date, dict] = {}
    old_seconds = 0.0
    old_comparisons = 0
    old_corpus_last = 0
    for d in dates:
        day = by_date.get(d, [])
        ai = [r for r in day if r.ai_relevance == "ai_candidate"]
        canonical = [r for r in ai if r.canonical_story_id is None]
        old_history = {sid for sid, ed in primary_episode_date.items() if ed < d}
        pool = [r for r in canonical if r.id not in old_history]
        old_corpus_last = len(old_history)

        started = time.perf_counter()
        flagged: dict[int, tuple[int, float]] = {}
        if old_history:
            hist_rows = {r.id: r for r in rows if r.id in old_history}
            new_texts = {r.id: get_comparable_text(r.raw_content, r.raw_summary, r.title) for r in pool}
            hist_texts = {i: get_comparable_text(r.raw_content, r.raw_summary, r.title) for i, r in hist_rows.items()}
            new_ids = [i for i, t in new_texts.items() if t is not None]
            hist_ids = [i for i, t in hist_texts.items() if t is not None]
            if new_ids and hist_ids:
                matrix = compute_cross_corpus_similarity([new_texts[i] for i in new_ids], [hist_texts[i] for i in hist_ids])
                old_comparisons += len(new_ids) * len(hist_ids)
                for ri, sid in enumerate(new_ids):
                    col = int(matrix[ri].argmax())
                    if float(matrix[ri, col]) >= CONTENT_SIMILARITY_THRESHOLD:
                        flagged[sid] = (hist_ids[col], float(matrix[ri, col]))
        old_seconds += time.perf_counter() - started

        prev_eligible = [r for r in pool if r.source_sufficiency != "insufficient"]
        date_rows[d] = {
            "date": d.isoformat(),
            "collected": len(day),
            "ai_candidates": len(ai),
            "previous_duplicates": len(ai) - len(canonical),
            "previous_historical_repeats": len(flagged),
            "previous_eligible": len(prev_eligible),
            "previous_selected": len(selected_on.get(d, ())),
            "_pool": pool,
            "_flagged": flagged,
            "_prev_eligible_ids": {r.id for r in prev_eligible},
        }
        for r in pool:
            candidates[r.id] = {"row": r, "date": d, "old_flag": flagged.get(r.id)}

    ids = sorted(candidates)
    if args.limit and args.limit < len(ids):
        stride = len(ids) / args.limit
        ids = [ids[int(i * stride)] for i in range(args.limit)]
    sampled = set(ids)

    # ---- new detector ---------------------------------------------------------------------
    embedder = default_embedder(persist=True)
    stats = Stats()
    corpus_rows = [(sid, ed) for sid, ed in published_primary_rows(db, args.corpus_mode == "approved+draft")
                   if not max_id or sid <= max_id]
    corpus = HistoricalCorpus(load_features(db, [sid for sid, _ in corpus_rows]), dict(corpus_rows), embedder)
    verdicts = detect_stories(db, ids, corpus, embedder, {i: candidates[i]["date"] for i in ids}, stats)
    embedder.save()
    stats.runtime_seconds += corpus.embed_seconds
    corpus_final = len(corpus.visible_mask(max(dates)).nonzero()[0]) if dates and len(corpus) else 0

    titles = {r.id: r.title for r in rows}
    story_rows = []
    for sid in ids:
        c = candidates[sid]
        r, d, v = c["row"], c["date"], verdicts.get(sid)
        flag = c["old_flag"]
        new_result = "duplicate" if (v and v.is_duplicate) else "new_development"
        old_result = (
            "historical_repeat" if flag
            else ("eligible" if r.source_sufficiency != "insufficient" else "insufficient")
        )
        story_rows.append({
            "story_id": sid,
            "date": d.isoformat(),
            "title": r.title,
            "previous_result": old_result,
            "previous_match": flag[0] if flag else None,
            "previous_tfidf": round(flag[1], 3) if flag else None,
            "new_result": new_result,
            "matched_story_id": v.matched_story_id if v else None,
            "matched_title": titles.get(v.matched_story_id) if v else None,
            "similarity": round(v.similarity, 3) if v else None,
            "development_match": v.decision.development_match if v else "no related published story",
            "new_facts": v.decision.new_facts if v else [],
            "rule": v.decision.rule if v else "unrelated",
            "reason": v.decision.reason if v else "No published story is related to this one.",
            "content_basis": v.decision.content_basis if v else None,
            "previous_match_in_corpus": bool(flag and flag[0] in corpus.pos),
            "previously_selected": sid in selected_on.get(d, ()),
            "previously_backup": sid in backup_on.get(d, ()),
            "changed": (old_result == "historical_repeat") != (new_result == "duplicate"),
        })

    # ---- per-date table ---------------------------------------------------------------------
    per_date = []
    for d in dates:
        dr = date_rows[d]
        mine = [s for s in story_rows if s["date"] == d.isoformat()]
        dups = [s for s in mine if s["new_result"] == "duplicate"]
        eligible_ids = dr["_prev_eligible_ids"]
        blocked_selected = [s for s in dups if s["previously_selected"]]
        now_allowed = [s for s in mine if s["previous_result"] == "historical_repeat" and s["new_result"] != "duplicate"]
        regress = blocked_selected + [s for s in now_allowed if (s["similarity"] or 0) >= 0.80]
        per_date.append({
            **{k: v for k, v in dr.items() if not k.startswith("_")},
            "new_duplicates": len(dups),
            "new_new_development": len(mine) - len(dups),
            "changed_decisions": sum(1 for s in mine if s["changed"]),
            "previously_published_newly_blocked": len(blocked_selected),
            "previously_blocked_now_allowed": len(now_allowed),
            "potential_regressions": len(regress),
            "new_eligible": len(eligible_ids - {s["story_id"] for s in dups}),
        })

    total = lambda k: sum(r[k] for r in per_date)
    new_dups = [s for s in story_rows if s["new_result"] == "duplicate"]
    old_repeats = [s for s in story_rows if s["previous_result"] == "historical_repeat"]
    summary = {
        "method_version": METHOD_VERSION,
        "corpus_mode": args.corpus_mode,
        "dates": [d.isoformat() for d in dates],
        "stories_replayed": len(story_rows),
        "old_repeats": len(old_repeats),
        "new_duplicates": len(new_dups),
        "new_new_development": len(story_rows) - len(new_dups),
        "new_with_related_coverage": sum(1 for s in story_rows if s["new_result"] == "new_development" and s["matched_story_id"]),
        "missed_by_old": [s for s in new_dups if s["previous_result"] != "historical_repeat"],
        "false_by_old": [s for s in old_repeats if s["new_result"] != "duplicate"],
        "false_by_old_judged": [s for s in old_repeats if s["new_result"] != "duplicate" and s["previous_match_in_corpus"]],
        "old_match_outside_corpus": [s for s in old_repeats if not s["previous_match_in_corpus"]],
        "agree_duplicate": [s for s in old_repeats if s["new_result"] == "duplicate"],
        "eligible_to_duplicate": sum(1 for s in new_dups if s["previous_result"] == "eligible"),
        "repeat_to_new": sum(1 for s in old_repeats if s["new_result"] != "duplicate"),
        "previous_eligible_total": total("previous_eligible"),
        "new_eligible_total": total("new_eligible"),
        "previously_selected_blocked": [s for s in new_dups if s["previously_selected"]],
        "corpus_final_new": corpus_final,
        "corpus_final_old": old_corpus_last,
    }
    perf = {
        "stories_processed": stats.stories_processed,
        "historical_corpus_size": len(corpus),
        "candidate_comparisons": stats.candidate_comparisons,
        "semantic_comparisons": stats.semantic_comparisons,
        "total_runtime_seconds": round(stats.runtime_seconds, 2),
        "of_which_embedding_seconds": round(stats.embed_seconds + corpus.embed_seconds, 2),
        "average_seconds_per_candidate": round(stats.average_seconds, 4),
        "slowest": [{"story_id": sid, "seconds": round(sec, 4), "title": _title(titles.get(sid, ""))} for sec, sid in stats.slowest],
        "embeddings_computed": embedder.computed,
        "embeddings_from_cache": embedder.hits,
        "old_tfidf_runtime_seconds": round(old_seconds, 2),
        "old_tfidf_comparisons": old_comparisons,
        "wall_clock_seconds": round(time.perf_counter() - wall, 2),
    }

    db.rollback()  # nothing was written; end the read-only transaction explicitly
    db.close()
    return {"baseline": base, "per_date": per_date, "stories": story_rows, "summary": summary, "performance": perf,
            "sampled": bool(args.limit), "sample_size": len(sampled)}


# ---------------------------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------------------------

def _table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join("" if c is None else str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _story_table(stories: list[dict]) -> str:
    rows = [[
        s["story_id"], s["date"], _title(s["title"], 60), s["previous_result"].replace("_", " "),
        s["new_result"].replace("_", " "),
        f'{s["matched_story_id"]}: {_title(s["matched_title"] or "", 40)}' if s["matched_story_id"] else "-",
        s["similarity"] if s["similarity"] is not None else "-", s["development_match"],
        "; ".join(s["new_facts"][:2]) or "-", s["reason"],
    ] for s in stories]
    return _table(["Story ID", "Date", "Title", "Previous result", "New result", "Matched historical story",
                   "Similarity", "Development comparison", "New facts", "Reason"], rows)


def render_markdown(res: dict) -> str:
    b, s, p = res["baseline"], res["summary"], res["performance"]
    st = res["stories"]
    lines = [f"# Historical dedup replay ({s['method_version']}, corpus: {s['corpus_mode']})", "",
             f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}. Read-only: nothing was written to the database.", ""]
    if res["sampled"]:
        lines += [f"**Sample run:** {res['sample_size']} stories only.", ""]
    lines += ["## Baseline (live database)", "", _table(["Metric", "Value"], [[k.replace("_", " "), v] for k, v in b.items()]), ""]
    lines += ["## Per date", "", _table(
        ["Date", "Collected", "AI candidates", "Previous duplicates", "Previous historical repeats",
         "Previous eligible", "Previous selected", "New semantic duplicates", "New semantic new-development",
         "Changed decisions", "Previously published stories newly blocked", "Previously blocked stories now allowed",
         "Potential regressions"],
        [[r["date"], r["collected"], r["ai_candidates"], r["previous_duplicates"], r["previous_historical_repeats"],
          r["previous_eligible"], r["previous_selected"], r["new_duplicates"], r["new_new_development"],
          r["changed_decisions"], r["previously_published_newly_blocked"], r["previously_blocked_now_allowed"],
          r["potential_regressions"]] for r in res["per_date"]]), ""]

    old_corpus, new_corpus = s["corpus_final_old"], s["corpus_final_new"]
    lines += ["## Executive comparison", "", _table(["Metric", "Previous Dedup", "New Semantic Dedup", "Change"], [
        ["Historical corpus (last date)", f"{old_corpus} past primaries, any episode status",
         f"{new_corpus} primaries of approved/published episodes", f"{new_corpus - old_corpus:+d}"],
        ["Historical comparisons", f"{p['old_tfidf_comparisons']} TF-IDF pairs",
         f"{p['semantic_comparisons']} vector + {p['candidate_comparisons']} rule comparisons", "-"],
        ["Duplicate decisions (historical)", "0 (repeat flag was a soft signal)", s["new_duplicates"], f"+{s['new_duplicates']}"],
        ["Possible repeats", s["old_repeats"], 0, f"{-s['old_repeats']:+d}"],
        ["New developments", s["stories_replayed"] - s["old_repeats"], s["new_new_development"],
         f"{s['new_new_development'] - (s['stories_replayed'] - s['old_repeats']):+d}"],
        ["Eligible candidates", s["previous_eligible_total"], s["new_eligible_total"],
         f"{s['new_eligible_total'] - s['previous_eligible_total']:+d}"],
        ["Previously missed repeats", "-", len(s["missed_by_old"]), "-"],
        ["Previously false repeats", "-",
         f"{len(s['false_by_old'])} ({len(s['false_by_old_judged'])} judged a different development against the "
         f"same published story; {len(s['false_by_old']) - len(s['false_by_old_judged'])} matched a story outside "
         f"this corpus)", "-"],
        ["Runtime", f"{p['old_tfidf_runtime_seconds']} s", f"{p['total_runtime_seconds']} s "
         f"({p['of_which_embedding_seconds']} s embedding)", "-"]]), ""]

    def section(title, items, note=""):
        out = [f"### {title} ({len(items)})", ""]
        if note:
            out += [note, ""]
        out += [_story_table(items) if items else "_none_", ""]
        return out

    lines += ["## Highlights", ""]
    a = sorted([x for x in st if x["previous_result"] != "historical_repeat" and x["new_result"] == "duplicate"], key=lambda x: -(x["similarity"] or 0))
    bb = [x for x in st if x["previous_result"] == "historical_repeat" and x["new_result"] != "duplicate"]
    c = sorted([x for x in st if x["new_result"] == "new_development" and x["rule"] in SAME_SUBJECT_NEW_RULES],
               key=lambda x: -(x["similarity"] or 0))
    dd = [x for x in a if x["rule"] == "identical_source" or (x["similarity"] or 0) >= STRONG_MISS_SIM]
    lines += section("A. Previously allowed, now duplicate", a)
    lines += section("B. Previously repeat, now allowed", bb,
                     "Rows with no matched story had their old match in an episode that is not approved/published, "
                     "so it is not in this corpus (see the approved+draft run).")
    lines += section("C. Same company/product, correctly new development", c[:40],
                     "Shared subject with the earlier story, but a different or further development (top 40 by similarity).")
    lines += section("D. Same development previously missed (high-confidence subset of A)", dd)
    lines += section("Previously selected stories now blocked", s["previously_selected_blocked"],
                     "Stories that sat in a published/approved/draft episode and the new detector would have held back.")
    lines += section("Old repeats confirmed as duplicates", s["agree_duplicate"])
    lines += ["## Performance", "", _table(["Metric", "Value"], [
        ["Total stories processed", p["stories_processed"]], ["Historical corpus size", p["historical_corpus_size"]],
        ["Candidate comparisons (full rule evaluation)", p["candidate_comparisons"]],
        ["Semantic comparisons (candidate x corpus vectors)", p["semantic_comparisons"]],
        ["Total runtime", f"{p['total_runtime_seconds']} s"], ["Of which embedding", f"{p['of_which_embedding_seconds']} s"],
        ["Average per candidate", f"{p['average_seconds_per_candidate'] * 1000:.1f} ms"],
        ["Embeddings computed / from cache", f"{p['embeddings_computed']} / {p['embeddings_from_cache']}"],
        ["Old TF-IDF reconstruction", f"{p['old_tfidf_runtime_seconds']} s"]]), "",
        "Slowest cases: " + "; ".join(f"{x['story_id']} ({x['seconds'] * 1000:.1f} ms, {x['title']})" for x in p["slowest"]), ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only replay of the historical duplicate detector.")
    parser.add_argument("--date", help="replay a single collection date (YYYY-MM-DD)")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--limit", type=int, help="replay an evenly spaced sample of this many stories")
    parser.add_argument("--corpus-mode", choices=["approved", "approved+draft"], default="approved")
    parser.add_argument("--max-story-id", type=int, help="ignore stories above this id (test fixtures)")
    parser.add_argument("--out-dir", default="media/reports")
    args = parser.parse_args()

    res = run(args)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{args.corpus_mode.replace('+', '_')}"
    (out / f"dedup_replay_{stamp}.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    (out / f"dedup_replay_{stamp}.md").write_text(render_markdown(res), encoding="utf-8")
    print(f"wrote {out / f'dedup_replay_{stamp}.md'}")
    s = res["summary"]
    print(f"replayed {s['stories_replayed']} stories: {s['new_duplicates']} duplicate, "
          f"{s['new_new_development']} new development; old repeats {s['old_repeats']}; "
          f"runtime {res['performance']['total_runtime_seconds']} s")


if __name__ == "__main__":
    main()
