from dataclasses import dataclass
from datetime import datetime, timezone

from app.verification.engine import count_independent_outlets, verify_story


# ---------------------------------------------------------
# Source credibility weights.
#
# Rationale: official model/lab blogs and well-regarded technical
# journalism are weighted highest; general tech blogs slightly lower.
# This is a subjective, editable starting point -- not a claim of
# objective truth -- and should be revisited once we have real
# audience/engagement data (see architecture's Analytics Worker /
# Optimization Engine, which is a later phase).
# ---------------------------------------------------------
CREDIBILITY_WEIGHTS: dict[str, float] = {
    "OpenAI": 0.95,
    "Google AI": 0.95,
    "NVIDIA Blog": 0.90,
    "Microsoft Research Blog": 0.90,
    "MIT Technology Review AI": 0.90,
    "Hugging Face Blog": 0.85,
    "The Verge AI": 0.85,
    "Ars Technica AI": 0.85,
    "TechCrunch AI": 0.80,
    "VentureBeat AI": 0.75,
    "Google DeepMind News": 0.95,
    "Wired — Artificial Intelligence": 0.85,
    "Hacker News": 0.75,
    # Publishers resolved from Hacker News link-posts (see
    # app/sources/publisher_resolver.py) -- the actual outlet that
    # wrote the story, not the discovery channel.
    "The Guardian": 0.85,
    "The Register": 0.80,
    "MacRumors": 0.75,
    "IEEE Spectrum": 0.90,
    # First-party research blogs of AI labs/universities: same tier as
    # "Microsoft Research Blog" -- the organisation reporting its own work.
    "Google Research Blog": 0.90,
    "Amazon Science": 0.90,
    "Berkeley AI Research (BAIR)": 0.90,
}

# The source registry names a feed differently from the publisher name used above (which is
# what the publisher resolver produces for a Hacker News link-post). Both must score the same
# publisher the same, so the feed names resolve to the publisher's entry.
CREDIBILITY_ALIASES: dict[str, str] = {
    "The Guardian AI": "The Guardian",
    "IEEE Spectrum — Artificial Intelligence": "IEEE Spectrum",
}

# Fallback for any source not explicitly weighted above (e.g. a new
# source added to the registry but not yet rated here).
DEFAULT_CREDIBILITY = 0.60

# How many independent outlets covering the same story counts as
# "maximum momentum". 3+ outlets covering something is a strong
# real-world signal that a story matters, without requiring an
# unbounded count to keep climbing the score.
MOMENTUM_CAP = 3

# Final weighted blend. Recency dominates (this is a daily news
# show), credibility and AI-relevance strength both matter
# meaningfully, momentum provides a real but bounded boost -- see
# the sanity-check numbers run before this was written: a stale
# story with max momentum still loses to a fresh relevant one.
SCORE_WEIGHTS = {
    "recency": 0.35,
    "credibility": 0.25,
    "ai_relevance": 0.25,
    "momentum": 0.15,
}

# A flat nudge, not a 5th weighted pillar -- deliberately not folded
# into SCORE_WEIGHTS above (which already sum to 1.0 and were tuned
# against real headline pairs, see this file's own comments) so the
# Verification Engine's soft signal (see app/verification/engine.py)
# can't destabilize that existing balance. Applied once per story when
# verification_status == "verified"; "pending"/"unverified" get none.
VERIFICATION_BONUS = 0.05


def compute_recency_score(
    published_at: datetime,
    now: datetime,
    window_hours: float,
) -> float:
    """
    Linear decay from 1.0 (published right now) to 0.0 (published
    exactly `window_hours` ago). Anything older than the window
    clamps to 0.0 rather than going negative.
    """

    hours_ago = (now - published_at).total_seconds() / 3600.0

    score = 1.0 - (hours_ago / window_hours)

    return max(0.0, min(1.0, score))


def compute_credibility_score(source_name: str) -> float:
    """
    Static per-source credibility weight. See CREDIBILITY_WEIGHTS
    above for rationale and DEFAULT_CREDIBILITY for the fallback. An unrated source gets the
    neutral default (not a penalty): it is a prior, not evidence against the source.
    """

    name = CREDIBILITY_ALIASES.get(source_name, source_name)
    return CREDIBILITY_WEIGHTS.get(name, DEFAULT_CREDIBILITY)


def compute_momentum_score(duplicate_count: int, cap: int = MOMENTUM_CAP) -> float:
    """
    How many other outlets independently covered the same story (the caller passes the
    distinct-other-outlet count, see count_independent_outlets; a second item from the same
    publisher is not extra coverage). More coverage = more momentum, capped so one viral story
    doesn't mathematically dominate everything else.
    """

    if cap <= 0:
        return 0.0

    return max(0.0, min(1.0, duplicate_count / cap))


# Value of the AI-relevance component for a story the rules classifier has certified as AI
# (a rules "candidate", or a review story an editor promoted). The component measures how
# AI-relevant a story is; for these stories that is settled by the classification, so it is the
# full value rather than a number inferred from the legacy keyword list.
CLASSIFIED_AI_RELEVANCE = 1.0


def ai_relevance_input(
    ai_relevance_score: float | None,
    classifier_version: str | None = None,
    classifier_disposition: str | None = None,
    review_decision: str | None = None,
) -> float | None:
    """
    The AI-relevance input for compute_total_score.

    Rows with a persisted classifier result (classifier_version set) are ranked on that result,
    not on ai_relevance_score, which is the legacy keyword scorer's output and misses AI terms the
    classifier knows (ChatGPT, Copilot, Apple Intelligence...). A story is certified AI when the
    classifier said "candidate", or when it said "review" and an editor promoted it. Rows
    classified before the classifier existed have no classifier_version and keep using
    ai_relevance_score unchanged.

    Which stories are eligible for ranking at all is decided elsewhere (ai_relevance ==
    "ai_candidate"); this only chooses the value of the AI-relevance component.
    """
    if classifier_version is None:
        return ai_relevance_score
    if classifier_disposition == "candidate" or review_decision == "promoted":
        return CLASSIFIED_AI_RELEVANCE
    return ai_relevance_score


def compute_total_score(
    published_at: datetime | None,
    source_name: str,
    ai_relevance_score: float | None,
    duplicate_count: int,
    now: datetime,
    window_hours: float,
    verification_status: str = "pending",
) -> tuple[float, str]:
    """
    Compute the final weighted ranking score for a single story.
    Returns (total_score, human_readable_reason) so the reason can
    be stored for audit/explainability (same pattern as the AI
    relevance filter and the dedup filter).

    verification_status defaults to "pending" so this stays safe to
    call before every story has been processed by
    run_fact_extraction_and_verification -- only "verified" adds the
    bonus; "pending"/"unverified" both score identically (no bonus,
    not penalized either -- see app/verification/engine.py, this is a
    soft signal, not a gate).
    """

    # A story with no publish date can't get a recency score; treat
    # it as the worst case (0.0) rather than crashing or guessing.
    recency = (
        compute_recency_score(published_at, now, window_hours)
        if published_at is not None
        else 0.0
    )

    credibility = compute_credibility_score(source_name)

    # ai_relevance_score already comes out of the AI relevance filter
    # in the 0.0-1.0 range; reuse it directly as the "AI impact"
    # component rather than inventing a second scoring pass.
    ai_component = ai_relevance_score if ai_relevance_score is not None else 0.0

    momentum = compute_momentum_score(duplicate_count)

    verification_bonus = VERIFICATION_BONUS if verification_status == "verified" else 0.0

    total = (
        SCORE_WEIGHTS["recency"] * recency
        + SCORE_WEIGHTS["credibility"] * credibility
        + SCORE_WEIGHTS["ai_relevance"] * ai_component
        + SCORE_WEIGHTS["momentum"] * momentum
        + verification_bonus
    )

    reason = (
        f"recency={recency:.2f}(w={SCORE_WEIGHTS['recency']}), "
        f"credibility={credibility:.2f}(w={SCORE_WEIGHTS['credibility']}), "
        f"ai_relevance={ai_component:.2f}(w={SCORE_WEIGHTS['ai_relevance']}), "
        f"momentum={momentum:.2f}(w={SCORE_WEIGHTS['momentum']}, "
        f"independent_outlets={duplicate_count}), "
        f"verification={verification_status}(bonus={verification_bonus:.2f}) "
        f"=> total={total:.3f}"
    )

    return total, reason


@dataclass(frozen=True)
class EventMember:
    """One story of an event cluster (a canonical story plus the duplicates linked to it)."""
    story_id: int
    source_name: str
    published_at: datetime | None
    ai_relevance: float | None
    verification_status: str = "pending"
    # Could this story stand as the event's representative on its own (AI candidate, source
    # sufficient, not a historical duplicate, not already narrated)? Non-viable members still
    # count as outlets that covered the event.
    viable: bool = True


@dataclass(frozen=True)
class EventScore:
    total: float
    reason: str
    representative_id: int


def score_event(
    canonical: EventMember,
    duplicates: list[EventMember],
    now: datetime,
    window_hours: float,
) -> EventScore:
    """
    Score one news development. Deduplication keeps a single canonical story per development,
    and the canonical is not always the best-scoring member (title dedup keeps the oldest story).
    Ranking only ever sees canonicals, so a development whose canonical scores below the cutoff
    would vanish even when a newer duplicate would have made it. The event therefore scores as its
    best viable member; the canonical stays the story that is selected (no links change).

    Corroboration is the number of distinct OTHER outlets in the cluster, the same count
    Verification uses. The canonical keeps its persisted verification status; a duplicate
    standing in as representative gets the status verify_story derives for it.
    """
    cluster = [canonical, *duplicates]
    best: tuple[float, bool, int] | None = None
    best_member = canonical
    best_reason = ""
    best_total = 0.0
    canonical_total = 0.0
    for member in cluster:
        if member is not canonical and not member.viable:
            continue
        outlets = count_independent_outlets(
            member.source_name, [o.source_name for o in cluster if o is not member]
        )
        if member is canonical:
            status = member.verification_status
        else:
            status = verify_story(member.source_name, compute_credibility_score(member.source_name), outlets)[0]
        total, reason = compute_total_score(
            published_at=member.published_at,
            source_name=member.source_name,
            ai_relevance_score=member.ai_relevance,
            duplicate_count=outlets,
            now=now,
            window_hours=window_hours,
            verification_status=status,
        )
        if member is canonical:
            canonical_total = total
        # Highest score; the canonical wins a tie, then the lowest id (deterministic).
        key = (round(total, 9), member is canonical, -member.story_id)
        if best is None or key > best:
            best, best_member, best_reason, best_total = key, member, reason, total
    if best_member is not canonical:
        best_reason += (
            f" [event score from duplicate #{best_member.story_id} ({best_member.source_name}); "
            f"canonical #{canonical.story_id} alone scores {canonical_total:.3f}]"
        )
    return EventScore(total=best_total, reason=best_reason, representative_id=best_member.story_id)
