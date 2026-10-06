"""
The deterministic decision layer of the historical duplicate detector.

Editorial rule: a story is a duplicate only when it reports substantially the same development
5squareFeed has already published. Sharing the company, product, model, topic or event is not
enough. So embedding similarity only says "these two are about the same thing"; this module then
asks whether the *development* is the same, and every verdict carries the rule and the reason
that produced it.

`decide()` is pure: features and two similarity numbers in, a Decision out.
"""
import re
from dataclasses import dataclass, field

from app.dedup.features import (
    DISTINCT_TYPES,
    LIFECYCLE_TYPES,
    NewFacts,
    StoryFeatures,
    covers_type,
    new_facts,
    split_sentences,
    subject_overlap,
)

METHOD_VERSION = "semantic-v1"

DUPLICATE = "duplicate"
NEW_DEVELOPMENT = "new_development"

_FIRST_PERSON_RE = re.compile(r"\b(?:i|i've|i'm|i'd|my|me)\b")

_QUOTED_RE = re.compile(r'["“][^"”]{0,400}["”]')
_EXPLAINER_TITLE_RE = re.compile(r"^(?:here.s why|why|how|what|when|can|could|should|is|are|does|do|will)\b|\?$")

def _first_person(story: StoryFeatures) -> bool:
    """True when the opening of the article is written in the first person (outside quotations)."""
    opening = " ".join(split_sentences(story.article.lead)[:2]).lower()
    return bool(_FIRST_PERSON_RE.search(_QUOTED_RE.sub(" ", opening)))


# Headline+lead cosine below which two stories are not treated as related coverage at all.
RELATED_FLOOR = 0.70
# Cosine needed, per situation, to call two stories the same development.
SIM_LAUNCH = 0.80          # same launch/release, subject shared
SIM_LAUNCH_UNSURE = 0.88   # same launch but the subject could not be confirmed
SIM_DISTINCT = 0.85        # same pricing/funding/security/... development
SIM_INCREMENTAL = 0.84     # same feature/update/expansion/availability development
SIM_UNKNOWN = 0.90         # development type not readable from the headline
SIM_UNKNOWN_WITH_TITLE = 0.84
TITLE_SIM_UNKNOWN = 0.85
SIM_LIMITED = 0.80         # a side has no article text: headline evidence only
TITLE_SIM_LIMITED = 0.90
SIM_LIMITED_SAME_TYPE = 0.85  # headline-level match when both headlines state the same development
# Cosine at or above which two articles are treated as the same report whatever their framing.
SIM_NEAR_COPY = 0.92
# Headline cosine a same-type duplicate also needs, so two stories that merely share a subject and a
# development label (a CEO dinner vs a CEO sketch) are not merged on lead similarity alone.
TITLE_SIM_SAME_TYPE = 0.70
# Share of the new lead's words that the earlier article does not contain, above which the lead
# is reporting material the earlier article never covered.
LEAD_NOVELTY_MATERIAL = 0.55


@dataclass
class Decision:
    decision: str
    rule: str
    reason: str
    development_match: str
    new_facts: list[str] = field(default_factory=list)
    related: bool = True  # False when the pair is not related coverage and is not worth recording
    content_basis: str = "full"

    @property
    def is_duplicate(self) -> bool:
        return self.decision == DUPLICATE


def type_relation(new: StoryFeatures, old: StoryFeatures) -> tuple[str, tuple[str, ...]]:
    """("same" | "different" | "unknown", types). Types read from the headline are trusted; types
    read from the lead only count as agreement, never as disagreement."""
    n = {t for t in new.types if t != "other"}
    o = {t for t in old.types if t != "other"}
    shared = tuple(sorted(n & o))
    if shared:
        return "same", shared
    if n and o and new.type_source == "title" and old.type_source == "title":
        return "different", ()
    return "unknown", ()


def _label(new: StoryFeatures, old: StoryFeatures) -> str:
    return f"{'/'.join(new.types)} vs {'/'.join(old.types)}"


def _basis(new: StoryFeatures, old: StoryFeatures) -> str:
    order = ("title_only", "summary", "full")
    return min(new.article.quality, old.article.quality, key=order.index)


def decide(
    new: StoryFeatures,
    old: StoryFeatures,
    similarity: float,
    title_similarity: float,
) -> Decision:
    basis = _basis(new, old)

    def out(decision: str, rule: str, reason: str, match: str, facts: NewFacts | None = None,
            related: bool = True) -> Decision:
        return Decision(
            decision=decision,
            rule=rule,
            reason=reason,
            development_match=match,
            new_facts=facts.describe() if facts else [],
            related=related,
            content_basis=basis,
        )

    # 1. Same article, however it was collected.
    if (new.url and old.url and new.url == old.url) or (
        new.content_hash and old.content_hash and new.content_hash == old.content_hash
    ):
        return out(DUPLICATE, "identical_source", "Same source article as the earlier story.", "same article")

    # 2. Not about the same thing at all.
    if similarity < RELATED_FLOOR:
        return out(
            NEW_DEVELOPMENT, "unrelated",
            f"No related published coverage (closest story is only {similarity:.2f} similar).",
            "unrelated", related=False,
        )

    facts = new_facts(new, old)
    relation, shared_types = type_relation(new, old)

    # 3. Same topic, different subject (another product from the same company, another model).
    subject, subject_hits = subject_overlap(new, old)
    if subject == "disjoint":
        return out(
            NEW_DEVELOPMENT, "different_subject",
            "Related topic, but the headline concerns a different product, model or party than the "
            "earlier story.",
            "different subject", facts,
        )

    # 4. The headline reports a kind of development the earlier article never reported.
    for dev_type in new.types:
        if new.type_source == "title" and dev_type in DISTINCT_TYPES and not covers_type(old, dev_type):
            return out(
                NEW_DEVELOPMENT, "new_development_type",
                f"Same subject, but this reports a {dev_type} development; the earlier story did not "
                f"report one.",
                f"different: {_label(new, old)}", facts,
            )

    # 5. Both headlines state a development and the developments differ.
    if relation == "different":
        incremental = [t for t in new.types if t in LIFECYCLE_TYPES and t != "launch"]
        lifecycle_pair = all(t in LIFECYCLE_TYPES for t in new.types + old.types if t != "other")
        if lifecycle_pair and incremental and not facts.material and similarity >= SIM_INCREMENTAL:
            return out(
                DUPLICATE, "same_development_other_verb",
                f"Describes the earlier development with a different verb ({'/'.join(new.types)} vs "
                f"{'/'.join(old.types)}) and brings no new headline fact.",
                f"same: {_label(new, old)} (wording only)", facts,
            )
        reason = (
            f"Same subject, but a different development: {'/'.join(new.types)} follows the earlier "
            f"{'/'.join(old.types)}."
        )
        return out(NEW_DEVELOPMENT, "different_development", reason, f"different: {_label(new, old)}", facts)

    # 5b. A different kind of report about the same subject: a first-person account or an
    # explainer is commentary on the earlier development, not a second report of it. A near-copy
    # of the earlier article is still the same report.
    if similarity < SIM_NEAR_COPY:
        if _first_person(new):
            return out(
                NEW_DEVELOPMENT, "first_hand_account",
                "Same subject, but this is a first-person account (using, attending or testing it), "
                "not a report of the earlier development.",
                "different: first-hand account", facts,
            )
        if _EXPLAINER_TITLE_RE.search(new.title.lower()) and not _EXPLAINER_TITLE_RE.search(old.title.lower()):
            return out(
                NEW_DEVELOPMENT, "explainer_coverage",
                "Same subject, but the headline is an explainer or analysis of it rather than a report "
                "of the earlier development.",
                "different: explainer", facts,
            )

    # 6. One side has no article text: only headline evidence is available.
    if basis == "title_only":
        if (
            relation != "different"
            and subject == "shared"
            and not facts.numbers
            and (
                (similarity >= SIM_LIMITED and title_similarity >= TITLE_SIM_LIMITED)
                or (relation == "same" and similarity >= SIM_LIMITED_SAME_TYPE)
            )
        ):
            return out(
                DUPLICATE, "headline_evidence",
                f"Article text unavailable; headline ({title_similarity:.2f}) and lead ({similarity:.2f}) "
                f"match the earlier story on the same subject ({', '.join(subject_hits[:3])}).",
                "same: headline-level", facts,
            )
        return out(
            NEW_DEVELOPMENT, "limited_evidence",
            "Article text unavailable and the headline alone does not show the same development; "
            "kept as new rather than suppressed on weak evidence.",
            "unconfirmed", facts,
        )

    subject_text = f" on {', '.join(subject_hits[:3])}" if subject_hits else ""

    # 7. Development types agree.
    if relation == "same":
        match = f"same: {'/'.join(shared_types)}"
        incremental = [t for t in shared_types if t in LIFECYCLE_TYPES and t != "launch"]
        distinct = [t for t in shared_types if t in DISTINCT_TYPES]

        if "launch" in shared_types:
            needed = SIM_LAUNCH if subject == "shared" else SIM_LAUNCH_UNSURE
            if facts.numbers:
                return out(
                    NEW_DEVELOPMENT, "launch_new_numbers",
                    "Same launch, but the headline carries figures the earlier story did not report.",
                    match, facts,
                )
            if similarity >= needed and title_similarity >= TITLE_SIM_SAME_TYPE:
                return out(
                    DUPLICATE, "same_launch",
                    f"Reports the same launch{subject_text} as the earlier story "
                    f"(lead similarity {similarity:.2f}); no new headline fact.",
                    match, facts,
                )
        elif distinct:
            if facts.numbers:
                return out(
                    NEW_DEVELOPMENT, "development_new_numbers",
                    f"Same kind of {distinct[0]} development, but with figures the earlier story did "
                    f"not report.",
                    match, facts,
                )
            if similarity >= SIM_DISTINCT and title_similarity >= TITLE_SIM_SAME_TYPE:
                return out(
                    DUPLICATE, "same_development",
                    f"Reports the same {distinct[0]} development{subject_text} as the earlier story "
                    f"(lead similarity {similarity:.2f}).",
                    match, facts,
                )
        elif incremental:
            if facts.material or facts.lead_novelty >= LEAD_NOVELTY_MATERIAL:
                return out(
                    NEW_DEVELOPMENT, "incremental_new_content",
                    f"Same kind of {incremental[0]} development, but it brings content the earlier "
                    f"article does not contain.",
                    match, facts,
                )
            if similarity >= SIM_INCREMENTAL:
                return out(
                    DUPLICATE, "same_development",
                    f"Reports the same {incremental[0]} development{subject_text} as the earlier story "
                    f"with nothing new (lead similarity {similarity:.2f}).",
                    match, facts,
                )
        return out(
            NEW_DEVELOPMENT, "insufficient_overlap",
            f"Same kind of development{subject_text}, but the stories are not close enough "
            f"({similarity:.2f}) to be the same report.",
            match, facts,
        )

    # 8. Development type unreadable from the headline: demand overwhelming evidence.
    strong = similarity >= SIM_UNKNOWN or (
        similarity >= SIM_UNKNOWN_WITH_TITLE and title_similarity >= TITLE_SIM_UNKNOWN
    )
    if strong and subject != "disjoint" and not facts.numbers and not facts.title_entities:
        return out(
            DUPLICATE, "near_identical_coverage",
            f"Headline does not state the development, but the story is near-identical to the earlier "
            f"one (lead {similarity:.2f}, headline {title_similarity:.2f}) with no new headline fact.",
            "same: inferred from text", facts,
        )
    return out(
        NEW_DEVELOPMENT, "development_unconfirmed",
        f"Related to an earlier story (lead {similarity:.2f}) but nothing shows it is the same "
        f"development.",
        "unconfirmed", facts,
    )
