import json
import math
import re

from app.extraction.fact_extractor import extract_companies, extract_events, extract_products
from app.models import NewsItem, StoryContent, StoryState


# ---------------------------------------------------------
# Deterministic, story-agnostic storyboard planning -- same
# explainable, no-LLM, no-API-key style as app/filters/ai_relevance.py,
# app/extraction/taxonomy.py, and app/extraction/fact_extractor.py.
# generate_storyboard() never branches on a specific story_id; it only
# ever reads the real per-sentence timing/facts already computed for
# whichever story it's given.
# ---------------------------------------------------------

SOURCE_CARD_DURATION_SECONDS = 3.0

COMPARISON_MARKERS = [" while ", " whereas ", " compared to ", " versus ", " vs. "]

# A story's narration is only ever allowed to close on a REAL
# conclusion the script itself contains -- never fabricated. If the
# final segment doesn't match one of these markers, the video closes
# on a plain source-attribution card instead (see the "closing scene"
# logic below).
TAKEAWAY_MARKERS = ["in summary", "overall,", "ultimately", "the result is", "this means"]

# Small, fixed technical-jargon list -- same "curated keyword set,
# not free-form understanding" style as app/filters/ai_relevance.py's
# AI_KEYWORDS, but for a different purpose (visual scene selection,
# not AI-relevance filtering).
CONCEPT_KEYWORDS = {
    "neural network", "algorithm", "architecture", "framework",
    "protocol", "benchmark", "infrastructure", "pipeline",
}

TAXONOMY_ACCENTS = {
    "major_news": (220, 90, 90),
    "research": (64, 156, 255),
    "security_policy": (235, 175, 60),
    "business": (95, 200, 145),
    "developer_tools": (175, 125, 235),
}
DEFAULT_ACCENT_COLOR = (64, 156, 255)

# app/extraction/fact_extractor.py's NUMERIC_CLAIM_RE/DATE_RE are
# tuned for $-prefixed/%/x-style claims and formal "Month Day, Year"
# dates -- confirmed live against real story content that neither
# catches bare "<number> million/billion/thousand" phrases or bare
# 4-digit years used as a projection marker ("By 2035"). This fills
# exactly that gap; everything else (companies/products/events/formal
# dates/$-or-%-or-x claims) is reused from fact_extractor.py directly,
# not reinvented.
BARE_NUMBER_UNIT_RE = re.compile(
    r"\b\d[\d,]*\.?\d*\s?(million|billion|thousand)\b"
    r"|\b(19|20)\d{2}\b",
    re.IGNORECASE,
)

HEADLINE_STAT_RE = re.compile(r"(\d[\d,]*)\s*(million|billion|thousand)", re.IGNORECASE)
_UNIT_LETTERS = {"million": "M", "billion": "B", "thousand": "K"}

TIER_RE = re.compile(r"\blevel\s+(\d+(?:-\d+)?)\b", re.IGNORECASE)
BY_YEAR_RE = re.compile(r"\bby\s+((?:19|20)\d{2})\b", re.IGNORECASE)
YEAR_RANGE_RE = re.compile(r"\bbetween\s+((?:19|20)\d{2})\s+and\s+((?:19|20)\d{2})\b", re.IGNORECASE)
BARE_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")

# A generic "<Proper Noun Phrase> <reporting verb>" pattern -- catches
# "ABI Research projects", "Omdia estimates", "the study found", etc.
# without hardcoding any specific organization name.
REPORTING_VERB_SOURCE_RE = re.compile(
    r"\b([A-Z][\w&.]*(?:\s+[A-Z][\w&.]*)*)\s+"
    r"(?:projects|estimates|predicts|forecasts|reports|found|shows|says)\b"
)

ENTITY_STOP_RE = re.compile(
    r"[,(]|\b(?:will|shall|is|are|was|were|has|have|had|between)\b", re.IGNORECASE
)

QUOTE_RE = re.compile(r"[\"“].+?[\"”]")

# "moving rapidly from research to large-scale deployment" -> ("research",
# "large-scale deployment"). Deliberately only 2 stages -- never fabricates
# an intermediate stage the sentence doesn't state.
FROM_TO_RE = re.compile(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:[.,;]|$)", re.IGNORECASE)

# Strips a leading question word so a title-derived kicker/header reads as
# a label rather than a truncated question -- removes words only, never
# adds any.
HEADER_FILLER_PREFIX_RE = re.compile(r"^(why|how|what|when)\s+", re.IGNORECASE)

# Category markers only -- never a specific manufacturer/model/capability.
ICON_KEYWORDS = {
    "vehicle": {"vehicle", "vehicles", "car", "cars", "autonomous vehicle", "autonomous vehicles", "av", "avs"},
    "robot": {"robot", "robots", "robotic"},
}

COMPARISON_STATE_INTRO_SECONDS = 1.6
COMPARISON_STATE_CONTEXT_SECONDS = 2.5
COMPARISON_STATE_MIN_HOLD_SECONDS = 1.0
# Matches app/qa/storyboard_qa.py's own _DURATION_CAPS["comparison"] -- the
# direct regression cap for "long static hold after count-up". A hold
# longer than this is split into multiple equal-length hold segments
# (same unchanging visual content, just multiple named states) rather
# than left as one long unbroken visual state.
COMPARISON_STATE_MAX_HOLD_SECONDS = 5.0


def _strip_tier(text: str) -> tuple[str, str | None]:
    match = TIER_RE.search(text)
    if not match:
        return text, None
    levels = match.group(1)
    tier = f"L{levels.split('-', 1)[0]}–L{levels.split('-', 1)[1]}" if "-" in levels else f"L{levels}"
    return text[:match.start()] + text[match.end():], tier


def _extract_date_field(text: str) -> str | None:
    match = YEAR_RANGE_RE.search(text)
    if match:
        return f"{match.group(1)}–{match.group(2)}"
    match = BY_YEAR_RE.search(text)
    if match:
        return f"By {match.group(1)}"
    match = BARE_YEAR_RE.search(text)
    if match:
        return match.group(1)
    return None


def _extract_source_field(text: str) -> str | None:
    match = REPORTING_VERB_SOURCE_RE.search(text)
    return match.group(1).strip() if match else None


def _extract_entity_field(tail: str) -> str:
    stop = ENTITY_STOP_RE.search(tail)
    phrase = (tail[:stop.start()] if stop else tail).strip()
    return phrase[:1].upper() + phrase[1:] if phrase else ""


def _extract_stat_fields(clause: str) -> dict:
    """
    Pulls a concise {stat, unit, entity, tier, date, source} shape out
    of one clause -- deterministic regex/keyword extraction, the same
    "concrete checkable pieces, not free-form understanding" spirit as
    app/extraction/fact_extractor.py. Used for both `statistic` and
    each side of a `comparison` scene.
    """
    tier_stripped, tier = _strip_tier(clause)
    date = _extract_date_field(clause)
    source = _extract_source_field(clause)

    match = HEADLINE_STAT_RE.search(tier_stripped)
    if not match:
        return {
            "stat": None, "unit": None,
            "entity": _extract_entity_field(tier_stripped),
            "tier": tier, "date": date, "source": source,
        }

    number, unit_word = match.groups()
    entity = _extract_entity_field(tier_stripped[match.end():])

    return {
        "stat": number.replace(",", ""),
        "unit": _UNIT_LETTERS.get(unit_word.lower(), ""),
        "entity": entity,
        "tier": tier,
        "date": date,
        "source": source,
    }


def _infer_icon(entity: str | None) -> str:
    """
    A generic CATEGORY marker only ("vehicle"/"robot"/"generic") --
    never implies a specific manufacturer, model, or capability. Falls
    back to "generic" on any uncertainty rather than guessing, so an
    unrelated future story's comparison scene never gets a wrong icon.
    """
    if not entity:
        return "generic"
    lowered = entity.lower()
    for icon, keywords in ICON_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return icon
    return "generic"


def _extract_progression_stages(text: str) -> list[str] | None:
    """
    "moving rapidly from research to large-scale deployment" ->
    ["RESEARCH", "LARGE-SCALE DEPLOYMENT"]. Returns None (no fabricated
    middle stage) when the sentence doesn't actually contain a real
    "from X to Y" relationship.
    """
    match = FROM_TO_RE.search(text)
    if not match:
        return None
    return [match.group(1).strip().upper(), match.group(2).strip().upper()]


def _derive_hero_kicker(title: str, max_words: int = 5) -> str:
    """
    A short, real-title-derived phrase for the hero's dominant visual
    -- only ever REMOVES words (a leading filler question-word, then
    the existing _shorten() truncation), never adds editorial framing
    not literally present in the title.
    """
    stripped = HEADER_FILLER_PREFIX_RE.sub("", title.strip())
    return _shorten(stripped, max_words=max_words)


def _derive_comparison_header(title: str, max_words: int = 6) -> str:
    """
    A short, real-title-derived NEUTRAL framing line for the
    comparison scene -- same word-removal-only derivation as
    _derive_hero_kicker, never manufactured editorial copy ("THE AI
    RACE" etc are never generated -- only real title words, trimmed).
    """
    stripped = HEADER_FILLER_PREFIX_RE.sub("", title.strip())
    return _shorten(stripped, max_words=max_words)


# A title-truncated header can end mid-clause on a verb (e.g. "...
# Demands...") right above two statistics -- easy to misread as "the
# stats below prove this claim", which the stats themselves don't
# establish. Whenever a comparison's own real data has this specific,
# checkable shape -- both sides carry a real date/timeframe field
# (i.e. this is inherently a forward-looking projection comparison)
# AND the real narration itself uses "deploy"/"deployment"/"deploying"
# -- a neutral structural label describing what the two independently-
# sourced figures actually are is used instead of the truncated title
# fragment. This is a deterministic, content-driven rule keyed on real
# source data (never on a specific story_id) -- it applies identically
# to any future comparison scene with this same real shape, and falls
# back to the ordinary title-derived header otherwise.
PROJECTED_DEPLOYMENT_HEADER = "PROJECTED DEPLOYMENT SCALE"
DEPLOYMENT_KEYWORD_RE = re.compile(r"\bdeploy", re.IGNORECASE)


def _comparison_header(title: str, narration_text: str, left: dict, right: dict, max_words: int = 6) -> str:
    if left.get("date") and right.get("date") and DEPLOYMENT_KEYWORD_RE.search(narration_text or ""):
        return PROJECTED_DEPLOYMENT_HEADER
    return _derive_comparison_header(title, max_words=max_words)


def _split_hold_states(total_duration: float) -> list[dict]:
    """
    A hold longer than COMPARISON_STATE_MAX_HOLD_SECONDS is broken into
    multiple equal-length "hold" states instead of one long unbroken
    one -- same unchanging content (no fabricated new visual concept),
    but no single named motion state sits on screen past the cap.
    Invariant: returned durations always sum to exactly `total_duration`.
    """
    if total_duration <= COMPARISON_STATE_MAX_HOLD_SECONDS:
        return [{"name": "hold", "duration": total_duration, "countup_seconds": None, "countup_side": None}]

    segment_count = math.ceil(total_duration / COMPARISON_STATE_MAX_HOLD_SECONDS)
    segment_duration = total_duration / segment_count
    return [
        {"name": "hold" if segment_count == 1 else f"hold_{index + 1}",
         "duration": segment_duration, "countup_seconds": None, "countup_side": None}
        for index in range(segment_count)
    ]


def _build_comparison_motion_states(duration: float, countup_seconds: float) -> list[dict]:
    """
    intro -> reveal_left -> reveal_right -> both_context -> hold (split
    into multiple bounded segments via _split_hold_states when long).
    reveal_left/reveal_right always get the IDENTICAL countup_seconds
    -- substantially equivalent visual treatment for both sides, never
    making one look more important than the other. Gracefully degrades
    for a short scene (drops both_context first, then falls back to
    the original single-ramp-plus-hold 2-state shape) so this stays
    correct for any future story's comparison scene, not tuned to one
    specific duration. Invariant: returned durations always sum to
    exactly `duration`.
    """
    reveal_total = countup_seconds * 2
    remaining = duration - COMPARISON_STATE_INTRO_SECONDS - reveal_total - COMPARISON_STATE_CONTEXT_SECONDS

    if remaining >= COMPARISON_STATE_MIN_HOLD_SECONDS:
        return [
            {"name": "intro", "duration": COMPARISON_STATE_INTRO_SECONDS, "countup_seconds": None, "countup_side": None},
            {"name": "reveal_left", "duration": countup_seconds, "countup_seconds": countup_seconds, "countup_side": "left"},
            {"name": "reveal_right", "duration": countup_seconds, "countup_seconds": countup_seconds, "countup_side": "right"},
            {"name": "both_context", "duration": COMPARISON_STATE_CONTEXT_SECONDS, "countup_seconds": None, "countup_side": None},
            *_split_hold_states(remaining),
        ]

    remaining_no_context = duration - COMPARISON_STATE_INTRO_SECONDS - reveal_total
    if remaining_no_context >= COMPARISON_STATE_MIN_HOLD_SECONDS:
        return [
            {"name": "intro", "duration": COMPARISON_STATE_INTRO_SECONDS, "countup_seconds": None, "countup_side": None},
            {"name": "reveal_left", "duration": countup_seconds, "countup_seconds": countup_seconds, "countup_side": "left"},
            {"name": "reveal_right", "duration": countup_seconds, "countup_seconds": countup_seconds, "countup_side": "right"},
            *_split_hold_states(remaining_no_context),
        ]

    # Original 2-state shape: one ramp (both sides count up together,
    # same as before this iteration) then one hold for the remainder.
    hold = max(0.0, duration - countup_seconds)
    return [
        {"name": "reveal_both", "duration": countup_seconds, "countup_seconds": countup_seconds, "countup_side": "both"},
        *_split_hold_states(hold),
    ]


def _split_comparison(text: str) -> tuple[str, str] | None:
    lowered = text.lower()
    for marker in COMPARISON_MARKERS:
        idx = lowered.find(marker)
        if idx != -1:
            return text[:idx], text[idx + len(marker):]
    return None


def _has_numeric_signal(text: str, claims: list[str]) -> bool:
    return bool(BARE_NUMBER_UNIT_RE.search(text)) or any(claim in text for claim in claims)


def _classify_segment(text: str, claims: list[str]) -> tuple[str, object | None]:
    """
    The decision table (checked in order): comparison -> statistic ->
    quote -> company/product -> concept -> key_fact -> hero (no
    signal). Returns (scene_type, extra) where `extra` carries
    whatever that branch already found (the split clauses, the
    matched company/product/event name) so the caller doesn't have to
    re-derive it.
    """
    comparison = _split_comparison(text)
    if comparison and _has_numeric_signal(comparison[0], claims) and _has_numeric_signal(comparison[1], claims):
        return "comparison", comparison

    if _has_numeric_signal(text, claims):
        return "statistic", None

    if QUOTE_RE.search(text):
        return "quote", None

    matched_companies = extract_companies(text)
    if matched_companies:
        return "company", matched_companies[0]

    matched_products = extract_products(text)
    if matched_products:
        return "product", matched_products[0]

    if any(keyword in text.lower() for keyword in CONCEPT_KEYWORDS):
        return "concept", None

    matched_events = extract_events(text)
    if matched_events:
        return "key_fact", matched_events[0]

    return "hero", None


def _shorten(text: str, max_words: int = 9) -> str:
    """
    A concise on-card headline derived from a sentence -- never the
    full sentence verbatim (the narration already carries the full
    explanation; see the module docstring / this project's
    "on-screen text is keywords, not paragraphs" rule).
    """
    trimmed = re.split(r"[,;]", text.strip(), maxsplit=1)[0]
    words = trimmed.split()
    return " ".join(words[:max_words]) + ("…" if len(words) > max_words else "")


def _build_scene(
    scene_type: str, extra, seg_texts: list[str], start: float, end: float, order: int,
    title: str, source_segment_indices: list[int],
) -> dict:
    narration_text = " ".join(seg_texts)
    base = {
        "scene_id": f"{scene_type}_{order}",
        "scene_type": scene_type,
        "order": order,
        "narration_text": narration_text,
        "start": start,
        "end": end,
        "duration": end - start,
        "silent": False,
        "source_segment_indices": source_segment_indices,
    }

    if scene_type == "hero":
        base["kicker"] = _derive_hero_kicker(title)
        base["motion"] = {"type": "zoom_in", "max_zoom": 1.12}
    elif scene_type == "comparison":
        left_clause, right_clause = extra
        left = _extract_stat_fields(left_clause)
        right = _extract_stat_fields(right_clause)
        left["icon"] = _infer_icon(left.get("entity"))
        right["icon"] = _infer_icon(right.get("entity"))
        base["left"] = left
        base["right"] = right
        base["header"] = _comparison_header(title, narration_text, left, right)
        base["visual_mode"] = "two_scale_signals"
        countup_seconds = 1.1
        base["motion"] = {
            "type": "split_reveal",
            "countup_seconds": countup_seconds,
            "states": _build_comparison_motion_states(end - start, countup_seconds),
        }
    elif scene_type == "statistic":
        base.update(_extract_stat_fields(narration_text))
        base["headline"] = _shorten(narration_text)
        base["motion"] = {"type": "count_up", "countup_seconds": 1.2}
    elif scene_type == "quote":
        quote_match = QUOTE_RE.search(narration_text)
        base["quote_text"] = quote_match.group(0) if quote_match else _shorten(narration_text)
        base["motion"] = {"type": "quote_reveal"}
    elif scene_type == "company":
        base["company"] = extra
        base["headline"] = _shorten(narration_text)
        base["motion"] = {"type": "lower_third"}
    elif scene_type == "product":
        base["product"] = extra
        base["headline"] = _shorten(narration_text)
        base["motion"] = {"type": "lower_third"}
    elif scene_type == "concept":
        base["headline"] = _shorten(narration_text)
        base["motion"] = {"type": "diagram_reveal"}
    elif scene_type == "key_fact":
        base["event_category"] = extra
        base["headline"] = _shorten(narration_text)
        stages = _extract_progression_stages(narration_text)
        base["stages"] = stages
        base["visual_mode"] = "progression" if stages else "headline"
        base["motion"] = {"type": "progression_reveal" if stages else "headline_reveal"}
    elif scene_type == "takeaway":
        base["headline"] = _shorten(narration_text)
        base["motion"] = {"type": "takeaway_reveal"}

    return base


def generate_storyboard(item: NewsItem, state: StoryState, content: StoryContent) -> dict:
    """
    Deterministic storyboard planning: no LLM, no randomness, no
    network -- the same input always produces the same output. Reads
    only real, already-computed data (edge-tts's own per-sentence
    timing, the existing fact-extraction/taxonomy output) rather than
    re-deriving or fabricating anything. Never references a specific
    story_id -- fully reusable across any story with production
    content already generated.
    """

    segments = json.loads(content.caption_segments) if content.caption_segments else []
    extracted_facts = json.loads(state.extracted_facts) if state.extracted_facts else {}
    claims = extracted_facts.get("claims", [])
    accent_color = list(TAXONOMY_ACCENTS.get(state.taxonomy_category, DEFAULT_ACCENT_COLOR))

    audio_duration = content.audio_duration_seconds or (segments[-1]["end"] if segments else 0.0)

    # Segment 0 is always treated as the opening hook, regardless of
    # its own content -- every video needs a hero beat before anything
    # else. Every later segment is classified on its own merits.
    classified: list[tuple[str, object | None, str]] = []
    for index, seg in enumerate(segments):
        if index == 0:
            classified.append(("hero", None, seg["text"]))
            continue
        scene_type, extra = _classify_segment(seg["text"], claims)
        classified.append((scene_type, extra, seg["text"]))

    # The narration is only allowed to close on a genuine conclusion
    # already present in the script -- check the LAST segment
    # specifically, never fabricate one.
    if len(segments) > 1 and any(marker in segments[-1]["text"].lower() for marker in TAKEAWAY_MARKERS):
        _, _extra, _text = classified[-1]
        classified[-1] = ("takeaway", _extra, _text)

    # Merge consecutive "hero" entries into one scene; every other
    # entry becomes its own scene. A cursor tracks contiguous timing
    # rather than trusting each segment's own start/end verbatim (real
    # edge-tts segment boundaries can have tiny (<0.1s) rounding gaps),
    # and the final narrated scene's end is pinned to the real,
    # ffprobe-measured audio duration rather than edge-tts's own
    # reported segment end, so scene timing and the eventual rendered
    # audio slice stay exactly aligned.
    scenes: list[dict] = []
    cursor = 0.0
    i = 0
    n = len(classified)
    order = 0

    while i < n:
        scene_type, extra, text = classified[i]

        if scene_type == "hero":
            group_texts = [text]
            group_indices = [i]
            group_end_index = i
            j = i + 1
            while j < n and classified[j][0] == "hero":
                group_texts.append(classified[j][2])
                group_indices.append(j)
                group_end_index = j
                j += 1

            end = audio_duration if group_end_index == n - 1 else segments[group_end_index]["end"]
            scenes.append(_build_scene("hero", None, group_texts, cursor, end, order, item.title, group_indices))
            cursor = end
            order += 1
            i = j
        else:
            end = audio_duration if i == n - 1 else segments[i]["end"]
            scenes.append(_build_scene(scene_type, extra, [text], cursor, end, order, item.title, [i]))
            cursor = end
            order += 1
            i += 1

    # Close on the real takeaway scene just built (if the last-segment
    # check above found one) -- otherwise append a plain, silent
    # source-attribution card. Never both, never neither.
    if not scenes or scenes[-1]["scene_type"] != "takeaway":
        scenes.append({
            "scene_id": "source_card",
            "scene_type": "source_card",
            "order": order,
            "narration_text": None,
            "start": cursor,
            "end": cursor + SOURCE_CARD_DURATION_SECONDS,
            "duration": SOURCE_CARD_DURATION_SECONDS,
            "silent": True,
            "source_segment_indices": [],
            "closing_line": f"Full story: {item.source_name}",
            "motion": {"type": "static_hold"},
        })
        cursor += SOURCE_CARD_DURATION_SECONDS

    return {
        "version": 1,
        "story_id": item.id,
        "taxonomy_category": state.taxonomy_category,
        "source_name": item.source_name,
        "accent_color": accent_color,
        "total_duration_seconds": cursor,
        "scenes": scenes,
    }
