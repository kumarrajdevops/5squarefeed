# Verification Engine (project.md's "VERIFICATION ENGINE" box: source
# checking, cross-source validation). Soft signal, per explicit user
# decision -- this never excludes a story from ranking. A hard
# "must be corroborated" gate would risk starving an already-small
# candidate pool (as few as 16-51 eligible stories/episode observed in
# this project's own live data) of single-source-but-legitimate news,
# like an official company blog announcing its own product. Instead,
# verification_status/verification_reason are recorded for editorial
# visibility (dashboard) and give ranking a small score nudge -- same
# "surface prominently, human decides" philosophy already used for
# Automated Video QA, which doesn't hard-block episode approval either.

# A source credible enough to count as a primary source for its own
# news -- no cross-source corroboration needed (see verify_story's
# docstring for why). Matches app/ranking/engine.py's own credibility
# scale (0.0-1.0).
PRIMARY_SOURCE_CREDIBILITY_THRESHOLD = 0.85


def count_independent_outlets(source_name: str, duplicate_source_names: list[str]) -> int:
    """How many distinct OTHER outlets carry the story. A second item from the same publisher
    (a video companion, a follow-up post) is the same source repeating itself, not confirmation."""
    own = (source_name or "").strip().lower()
    return len({(n or "").strip().lower() for n in duplicate_source_names} - {own, ""})


def verify_story(
    source_name: str,
    credibility_score: float,
    duplicate_count: int,
) -> tuple[str, str]:
    """
    Decide a story's verification status.

    Returns (status, reason) -- status is "verified" or "unverified".
    "pending" (not yet processed) is the DB column's own default, not
    a value this function returns.

    A story is "verified" if either:
      1. It's corroborated by at least one other independently-
         discovered outlet (duplicate_count >= 1 -- the same cross-
         source signal already used for ranking's "momentum" score,
         just as a status here rather than a continuous score).
      2. It comes from a source credible enough to be its own primary
         source (e.g. OpenAI's own blog announcing OpenAI's own
         product) -- requiring a second outlet to "confirm" a
         company's own announcement about itself would be an
         unreasonable bar, and would wrongly flag routine, completely
         legitimate single-source news as unverified.

    Everything else is "unverified" -- not excluded (see module
    docstring), just flagged for editorial awareness.
    """

    if duplicate_count >= 1:
        return (
            "verified",
            f"Cross-source confirmed by {duplicate_count} other outlet(s).",
        )

    if credibility_score >= PRIMARY_SOURCE_CREDIBILITY_THRESHOLD:
        return (
            "verified",
            f"Primary/official source ({source_name}, "
            f"credibility={credibility_score:.2f}) -- self-verifying "
            f"for its own news.",
        )

    return (
        "unverified",
        f"Single source ({source_name}, credibility={credibility_score:.2f}), "
        f"no cross-source confirmation yet.",
    )
