# Deterministic story categorization into a simple 5-category taxonomy
# (see proposal.md's "My proposed final taxonomy" section for the
# original, much larger 9-category + LLM-based clustering vision --
# this is a deliberately smaller, keyword-only slice of it: 5
# categories, reusing the event categories
# app/extraction/fact_extractor.py already extracts, no LLM involved
# anywhere, matching this project's hard "no LLM" rule).
#
# Labels only for now, by explicit user decision -- this does NOT
# change which 25 stories get selected (see app/tasks/ranking.py),
# just adds a visible category to every story. Whether a future pass
# turns this into an actual budget/diversity selection algorithm is an
# open, deliberately deferred question -- see TODO.md.

MAJOR_NEWS = "major_news"
RESEARCH = "research"
SECURITY_POLICY = "security_policy"
BUSINESS = "business"
DEVELOPER_TOOLS = "developer_tools"

CATEGORIES = [MAJOR_NEWS, RESEARCH, SECURITY_POLICY, BUSINESS, DEVELOPER_TOOLS]

# fact_extractor.py's EVENT_KEYWORDS categories, priority-ordered --
# first match wins (a story can match more than one event category but
# gets exactly one taxonomy label). "launch" is deliberately absent: a
# launch could be major news (a frontier model) or a small tool, and
# offers no signal either way on its own -- let the other rules (or
# the default) decide instead.
_EVENT_TO_CATEGORY = {
    "research": RESEARCH,
    "lawsuit_regulatory": SECURITY_POLICY,
    "safety_policy": SECURITY_POLICY,
    "funding": BUSINESS,
    "acquisition": BUSINESS,
    "partnership": BUSINESS,
    "personnel": BUSINESS,
}

# Hacker News's own community-post title conventions -- reliably
# developer/tool-shaped regardless of what they link to, unlike a
# generic HN submission of a mainstream news article (which should
# still get classified on its actual content/event signals below).
_HN_COMMUNITY_POST_PREFIXES = ("show hn:", "launch hn:", "ask hn:")


def classify_category(title: str, events: list[str], source_type: str) -> str:
    """
    Deterministic, priority-ordered category assignment:
      1. Hacker News's "Show HN:"/"Launch HN:"/"Ask HN:" post
         convention -- a strong, deliberate, title-based developer/
         tools signal, checked BEFORE content-event signals. Confirmed
         live this needed to be first, not second: a real "Show HN:"
         post about a quantized-model speedup got misclassified as
         Research because its body text's benchmark numbers tripped
         fact_extractor.py's "research" event keyword ("benchmark") --
         a coincidental keyword hit in body text is a weaker signal
         than an author's own explicit, deliberate title convention.
      2. Content-based event signals (research / security-policy /
         business) from fact_extractor.py's already-extracted events.
      3. Any other Hacker News submission with no stronger signal above
         -- a weaker fallback (proposal.md's own "Developer Radar tied
         to HN" framing), still Developer/Tools.
      4. Default: Major News -- the catch-all for general RSS coverage
         that didn't match anything more specific above.
    """

    normalized_title = title.strip().lower()
    if normalized_title.startswith(_HN_COMMUNITY_POST_PREFIXES):
        return DEVELOPER_TOOLS

    for event_category, taxonomy_category in _EVENT_TO_CATEGORY.items():
        if event_category in events:
            return taxonomy_category

    if source_type == "hackernews":
        return DEVELOPER_TOOLS

    return MAJOR_NEWS
