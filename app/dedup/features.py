"""
Deterministic story features for the historical duplicate detector: cleaned article text, the
development type (what happened), entities (who/what it happened to), and the facts a newer
article carries that an older one does not. No models are used here; the semantic embedding
lives in embedder.py and everything is combined in decision.py.
"""
import html
import re
from dataclasses import dataclass, field

from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

from app.extraction.fact_extractor import KNOWN_COMPANIES

# ---------------------------------------------------------
# Text preparation
# ---------------------------------------------------------

_BLOCK_TAG_RE = re.compile(r"</?(?:p|br|div|li|h[1-6]|tr|ul|ol|blockquote)\b[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])[\"')\]]?\s+(?=[\"'(\[]?[A-Z0-9$])|\n+")

# An article body below this is not enough to judge "what is new" from; the story is compared
# on its headline/summary and the decision says so.
FULL_CONTENT_MIN_WORDS = 120
SUMMARY_MIN_CHARS = 50
LEAD_SENTENCES = 4


def clean_text(text: str | None) -> str:
    """Strip markup/URLs and collapse whitespace. Hacker News link posts store raw HTML anchors;
    left in, their markup alone makes two unrelated posts look identical."""
    if not text:
        return ""
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _URL_RE.sub(" ", text)
    text = re.sub(r"[ \t\r\f\v ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s and len(s.strip()) > 3]


@dataclass
class ArticleText:
    """What the detector reads for one story, and how much of it is real article text."""

    title: str
    body: str
    quality: str  # "full" | "summary" | "title_only"
    lead: str = ""

    @property
    def words(self) -> int:
        return len(self.body.split())


def build_article_text(
    title: str,
    raw_content: str | None,
    raw_summary: str | None,
    fetch_status: str | None = None,
) -> ArticleText:
    """Reuse what extraction already stored: raw_content (full article when the fetch succeeded,
    otherwise whatever the feed carried), then raw_summary, then the headline alone."""
    title = clean_text(title)
    content = clean_text(raw_content)
    summary = clean_text(raw_summary)

    body, quality = "", "title_only"
    if len(content.split()) >= FULL_CONTENT_MIN_WORDS and fetch_status in (None, "success", "partial"):
        body, quality = content, "full"
    elif len(content) >= SUMMARY_MIN_CHARS or len(summary) >= SUMMARY_MIN_CHARS:
        body, quality = (content if len(content) >= len(summary) else summary), "summary"
    elif content or summary:
        body = content or summary

    lead = " ".join(split_sentences(body)[:LEAD_SENTENCES])
    return ArticleText(title=title, body=body, quality=quality, lead=lead)


# ---------------------------------------------------------
# Development type -- a small closed set, not an ontology.
# "distinct" types are self-contained events (a new price, a lawsuit ruling...) that are never the
# same development as a product launch even when the same product is involved; "lifecycle" types
# describe the product's own story arc, where headline verbs overlap freely (launch/introduce/
# roll out) and the article content has to decide.
# ---------------------------------------------------------

DISTINCT_TYPES = (
    "pricing", "funding", "acquisition", "partnership", "security", "lawsuit", "policy",
    "personnel", "shutdown", "research", "benchmark",
)
LIFECYCLE_TYPES = ("launch", "feature", "update", "availability", "expansion")

_DEV_PATTERNS: list[tuple[str, re.Pattern]] = [
    (t, re.compile(p, re.I))
    for t, p in [
        ("security", r"vulnerab|\bflaws?\b|\bbreach|exploit|\bhack(?:ed|ers?|ing)?\b|\bleak(?:s|ed)?\b|malware|ransomware|zero-day|\bcve-\d|security (?:hole|bug|issue|risk|incident|patch|fix)|phishing|data exposure|\bbugs?\b|\bspyware"),
        ("pricing", r"\bpric(?:e|es|ed|ing)\b|\bcosts?\b|subscription|per month|/month|/year|free (?:tier|plan)|paid plans?|\bfees?\b|cheaper|discounts?|\bplans? (?:start|from)|\$\d[\d.,]*\s?(?:a|per) (?:month|year|seat|user)"),
        ("funding", r"\braises?\b|\braised\b|funding|series [a-f]\b|valuation|valued at|\binvest(?:s|ed|ment|ments|ing)?\b|\bipo\b|seed round|backed by"),
        ("acquisition", r"\bacquir|\bacquisition|\bbuys\b|\bbought\b|takeover|\bmerger|\bmerges\b|acqui-?hire"),
        ("partnership", r"\bpartner(?:s|ed|ship|ships|ing)?\b|teams? up|collaborat|joint venture|\balliance\b|signs? (?:a )?(?:deal|agreement|contract)|\binks\b"),
        ("lawsuit", r"lawsuits?|\bsues?\b|\bsued\b|\bsuing\b|\bcourt\b|\bjudge\b|\bruling\b|antitrust|\bsettle(?:s|d|ment)?\b|dismiss|injunction|\btrial\b|\bappeals?\b|\bfined\b|\bprobe\b|\bcharged\b"),
        ("policy", r"regulat|\blegislat|\bbill\b|\bbans?\b|\bbanned\b|executive order|congress|senate|\blaw(?:s|makers)?\b|\bpolicy\b|\bpolicies\b|\bcompliance\b|\beu act\b|\bact\b"),
        ("personnel", r"\bceo\b|\bcto\b|chief [a-z]+ officer|steps? down|stepped down|resign|\bquits?\b|\bleaves\b|\bdeparts?\b|\bdeparture\b|\bhires?\b|\bhired\b|appoint|\bjoins\b|poach|\bfired\b|layoffs?|\bexits?\b"),
        ("shutdown", r"shuts? down|shutting down|discontinu|\bsunsets?\b|\bretir(?:es|ing|ed)\b|kills? off|ends? support|winds? down|deprecat|pulls? the plug"),
        ("research", r"\bstudy\b|\bstudies\b|researchers?|\bpaper\b|research (?:finds|shows|reveals)|\bdiscover(?:s|ed|y|ies)?\b|finds that|new (?:method|technique|approach)|breakthrough|arxiv"),
        ("benchmark", r"benchmark|leaderboard|outperform|state-of-the-art|\bsota\b|\bevals?\b|ranks? (?:first|#|no\.)|\bscores?\b"),
        ("availability", r"now available|available (?:in|to|for|on|today|starting)|(?:rolls?|rolling|rolled) out\b.{0,50}\b(?:to|in|for)\b|\brollout\b|generally available|general availability|waitlist|expands? (?:to|access)|\barrives? (?:in|on|for)\b|\bcomes? to\b|coming to"),
        ("expansion", r"\bexpand(?:s|ed|ing)\b|\bexpansion\b|opens? (?:a |new |its )?(?:office|data cent|campus|hub|facility)|data cent(?:er|re)s?|new offices?|scales? up|\binternational"),
        ("feature", r"\badds?\b|\badding\b|new features?|\bfeatures?\b|capabilit|now (?:supports?|lets?|can|allows?)|\blets (?:users|you)\b|\bgains?\b|\bintegrat|support for|\bbrings?\b|\benables?\b|new (?:mode|tool|ability|abilities)"),
        ("update", r"\bupdates?\b|\bupdated\b|\bupgrades?\b|\bupgraded\b|new version|\bv\d|version \d|revamp|redesign|overhaul|\brefresh|\bimproves?\b"),
        ("launch", r"\blaunch(?:es|ed|ing)?\b|\bintroduc(?:es|ed|ing)\b|(?:rolls?|rolling|rolled) out\b|\bunveil|\bdebut|\breleas(?:es|ed|ing)\b|open[- ]sources?\b|\bships?\b|\bshipped\b|\bnew (?:model|product|app|tool|chip|agent)\b|\bis here\b|\bpublishes\b"),
    ]
]
# "announces"/"reveals" report that something happened; they only name the development when no
# other type is present ("announces pricing" is pricing, not a launch).
_WEAK_LAUNCH_RE = re.compile(r"\bannounc(?:es|ed|ing)\b|\breveals?\b|\bpresents?\b|\bdetails\b|\bshows off\b", re.I)


def development_types(title: str, lead: str = "") -> tuple[tuple[str, ...], str]:
    """(types, source). Types come from the headline; when the headline names none, from the first
    lead sentence (source "lead", a weaker signal); ("other",) with source "none" otherwise."""
    for source, text in (("title", title), ("lead", (split_sentences(lead) or [""])[0])):
        found = [name for name, pattern in _DEV_PATTERNS if pattern.search(text)]
        if not found and _WEAK_LAUNCH_RE.search(text):
            found = ["launch"]
        if found:
            return tuple(found), source
    return ("other",), "none"


def covers_type(features: "StoryFeatures", dev_type: str) -> bool:
    """Does the earlier article itself report this kind of development in its headline or lead?"""
    pattern = dict(_DEV_PATTERNS)[dev_type]
    return bool(pattern.search(features.article.title) or pattern.search(features.article.lead))


# ---------------------------------------------------------
# Entities
# ---------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-.][A-Za-z0-9]+)*")
_ENTITY_STOP = {
    "the", "this", "that", "these", "those", "a", "an", "in", "on", "at", "for", "to", "of", "and",
    "or", "but", "as", "by", "with", "from", "it", "its", "is", "are", "was", "were", "how", "why",
    "what", "when", "where", "who", "which", "will", "can", "new", "now", "today", "here", "there",
    "his", "her", "their", "our", "your", "my", "we", "you", "they", "he", "she", "i", "if", "so",
    "after", "before", "over", "under", "more", "most", "all", "one", "two", "three", "also", "not",
    "no", "yes", "just", "about", "into", "than", "then", "some", "any", "every", "each", "other",
    "inside", "report", "reports", "says", "said", "say", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "january", "february", "march", "april", "may",
    "june", "july", "august", "september", "october", "november", "december", "ai", "ceo", "cto",
    "api", "us", "uk", "eu", "app", "apps", "llm", "llms", "gpu", "gpus", "ml", "it's", "faq",
    "update", "updates", "exclusive", "breaking", "opinion", "analysis", "watch", "review",
    "ask", "show", "hn", "tell", "u.s", "u.s.", "ap", "inc", "ltd", "corp", "co", "pm", "am",
}
# Extra company names beyond the fact extractor's list; matched case-insensitively as tokens.
_EXTRA_COMPANIES = {
    "apple", "xai", "openai", "anthropic", "google", "microsoft", "meta", "amazon", "nvidia",
    "tesla", "samsung", "sony", "oracle", "uber", "spotify", "netflix", "disney", "intel", "amd",
    "huawei", "bytedance", "tiktok", "snap", "pinterest", "reddit", "x", "twitter", "github",
    "linkedin", "paypal", "stripe", "shopify", "cloudflare", "palantir", "deepmind", "mozilla",
    "cursor", "figma", "notion", "slack", "zoom", "siri",
}
COMPANY_TOKENS = {w for name in KNOWN_COMPANIES for w in name.split()} | _EXTRA_COMPANIES
_COMPANY_FILLER = {"ai", "labs", "research", "blog", "news", "hugging", "face", "scale", "character"}


def _is_title_case(title: str) -> bool:
    words = [w for w in re.findall(r"[A-Za-z]+", title) if len(w) > 3]
    return len(words) >= 3 and sum(w[0].isupper() for w in words) / len(words) >= 0.7


def _looks_proper(token: str) -> bool:
    return bool(
        re.search(r"\d", token)
        or re.search(r"[a-z][A-Z]", token)
        or (token.isupper() and len(token) > 1)
        or (token[0].isupper() and any(c.isupper() for c in token[1:]))
    )


def _is_entity(tok: str, low: str, position: int, title_cased: bool, body_caps: set[str]) -> bool:
    if low in _ENTITY_STOP or len(low) < 2:
        return False
    if _looks_proper(tok) or low in COMPANY_TOKENS:
        return True
    if not tok[0].isupper():
        return False
    if position == 0:
        return low in body_caps
    return (not title_cased) or low in body_caps


def _body_caps(body: str) -> set[str]:
    caps: set[str] = set()
    for sentence in split_sentences(body):
        for tok in _TOKEN_RE.findall(sentence)[1:]:
            if tok[0].isupper():
                caps.add(tok.lower())
    return caps


def extract_title_entities(title: str, body: str = "") -> set[str]:
    """Lower-cased entity tokens of the headline: CamelCase / digit-bearing product and model
    names (iPhone, GPT-6.1), all-caps, known companies, and capitalised words that are not
    sentence-initial common words. In a Title-Cased headline a plain capitalised word only counts
    when the body also capitalises it mid-sentence."""
    caps = _body_caps(body)
    title_cased = _is_title_case(title)
    return {
        tok.lower().rstrip(".")
        for i, tok in enumerate(_TOKEN_RE.findall(title))
        if _is_entity(tok, tok.lower().rstrip("."), i, title_cased, caps)
    }


def extract_entities(text: str) -> set[str]:
    """Entity tokens of running text (lead / body)."""
    found: set[str] = set()
    for sentence in split_sentences(text):
        for i, tok in enumerate(_TOKEN_RE.findall(sentence)):
            low = tok.lower().rstrip(".")
            if low in _ENTITY_STOP or len(low) < 2:
                continue
            if _looks_proper(tok) or low in COMPANY_TOKENS or (i > 0 and tok[0].isupper()):
                found.add(low)
    return found


def specific_entities(entities: set[str]) -> set[str]:
    """Entities that identify a product, model or person rather than a company -- the part of
    'OpenAI launches Dots' that says which story it is."""
    return {e for e in entities if e not in COMPANY_TOKENS and e not in _COMPANY_FILLER}


# ---------------------------------------------------------
# Terms and numbers (for the "what is new" comparison)
# ---------------------------------------------------------

_NUMBER_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s?(?:million|billion|trillion|bn|m|k|b)?\b"
    r"|\b\d+(?:\.\d+)?\s?(?:%|percent|x\b|million|billion|trillion|bn|gb|tb|mb|ghz|tokens?|"
    r"parameters?|users|customers|employees|countries|languages)"
    r"|\b\d+(?:\.\d+){1,3}\b"
    r"|\b\d{1,3}(?:,\d{3})+\b",
    re.I,
)
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

_GENERIC_TERMS = {
    "new", "says", "said", "report", "reports", "first", "latest", "major", "big", "better", "just",
    "company", "users", "user", "people", "model", "models", "tool", "tools", "product", "products",
    "platform", "service", "services", "technology", "tech", "artificial", "intelligence", "today",
    "year", "years", "week", "day", "time", "way", "world", "make", "makes", "made", "will", "get",
    "gets", "using", "use", "uses", "used", "show", "shows", "could", "would", "may", "might",
    "like", "more", "most", "set", "plans", "plan", "wants", "want", "take", "takes", "help",
    "helps", "comes", "ahead", "back", "next", "own", "real", "good", "best", "top",
}
_DEV_WORD_RE = re.compile(
    "|".join(p.pattern for _, p in _DEV_PATTERNS) + "|" + _WEAK_LAUNCH_RE.pattern, re.I
)
_STOP = set(ENGLISH_STOP_WORDS) | _GENERIC_TERMS


def stem(word: str) -> str:
    for suffix in ("ations", "ation", "ing", "ies", "ed", "es", "ly", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def content_stems(text: str) -> set[str]:
    return {stem(t) for t in (w.lower() for w in _TOKEN_RE.findall(text)) if t not in _STOP and len(t) > 2}


_PLAIN_NUMBER_RE = re.compile(r"(?<![\w.-])\d+(?:\.\d+)?(?![\w.-])")


def title_numbers(title: str) -> set[str]:
    """Every standalone number in a headline ('7 days', '$20'); model names like GPT-6.1 are
    part of a token and are not numbers here."""
    out = numbers_in(title)
    out |= {n for n in _PLAIN_NUMBER_RE.findall(title) if not _YEAR_RE.match(n)}
    return out


def numbers_in(text: str) -> set[str]:
    out = set()
    for match in _NUMBER_RE.findall(text):
        norm = re.sub(r"[\s,$]", "", match.lower())
        norm = norm.replace("percent", "%")
        if _YEAR_RE.match(norm) or not norm:
            continue
        out.add(norm)
    return out


# ---------------------------------------------------------
# Per-story feature bundle
# ---------------------------------------------------------


@dataclass
class StoryFeatures:
    story_id: int | None
    article: ArticleText
    url: str | None = None
    content_hash: str | None = None
    source_name: str | None = None
    types: tuple[str, ...] = ("other",)
    type_source: str = "none"  # "title" | "lead" | "none"
    title_entities: set[str] = field(default_factory=set)
    lead_entities: set[str] = field(default_factory=set)
    body_stems: set[str] = field(default_factory=set)
    title_stems: set[str] = field(default_factory=set)
    body_lower: str = ""
    head_lower: str = ""  # headline + lead, lower-cased

    @property
    def title(self) -> str:
        return self.article.title

    @property
    def specific_title_entities(self) -> set[str]:
        return specific_entities(self.title_entities)

    @property
    def companies(self) -> set[str]:
        return {e for e in self.title_entities | self.lead_entities if e in COMPANY_TOKENS}

    @property
    def embed_text(self) -> str:
        """What gets embedded: the headline plus the lead, where a news article states the
        development. Later paragraphs are background that makes every story on a product alike."""
        return f"{self.article.title}. {self.article.lead}"[:1500]


def build_features(
    story_id: int | None,
    title: str,
    raw_content: str | None = None,
    raw_summary: str | None = None,
    fetch_status: str | None = None,
    url: str | None = None,
    content_hash: str | None = None,
    source_name: str | None = None,
) -> StoryFeatures:
    article = build_article_text(title, raw_content, raw_summary, fetch_status)
    publisher = {w.lower() for w in _TOKEN_RE.findall(source_name or "")}
    title_entities = extract_title_entities(article.title, article.body) - publisher
    types, type_source = development_types(article.title, article.lead)
    # Headline words that carry the development itself: not entities, not the verbs that name a
    # development type.
    title_stem_text = " ".join(
        w for w in _TOKEN_RE.findall(article.title)
        if w.lower() not in title_entities and not _DEV_WORD_RE.search(w)
    )
    return StoryFeatures(
        story_id=story_id,
        article=article,
        url=url,
        content_hash=content_hash,
        source_name=source_name,
        types=types,
        type_source=type_source,
        title_entities=title_entities,
        lead_entities=(extract_entities(article.lead) if article.lead else set()) - publisher,
        body_stems=content_stems(article.title + " " + article.body),
        title_stems=content_stems(title_stem_text),
        body_lower=(article.title + " " + article.body).lower(),
        head_lower=(article.title + " " + article.lead).lower(),
    )


# ---------------------------------------------------------
# Relations between two stories
# ---------------------------------------------------------


def _mentions(entity: str, haystack: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(entity) + r"(?![a-z0-9])", haystack) is not None


def subject_overlap(new: StoryFeatures, old: StoryFeatures) -> tuple[str, list[str]]:
    """Do the two stories concern the same product/model/person? ("shared", entities) when a
    specific headline entity of either story appears in the other's headline or lead;
    ("disjoint", []) when each names specific entities and they never meet, or when they name
    different companies and nothing specific; ("unknown", []) when neither headline names
    anything specific enough to tell."""
    new_spec, old_spec = new.specific_title_entities, old.specific_title_entities
    hits = sorted(
        {e for e in new_spec if _mentions(e, old.head_lower)}
        | {e for e in old_spec if _mentions(e, new.head_lower)}
    )
    if hits:
        return "shared", hits
    if new_spec and old_spec:
        return "disjoint", []
    if new_spec or old_spec:
        # One headline names a specific product, the other only a company: a shared company
        # keeps this open; different companies (or none shared) make it a different story.
        if new.companies and old.companies and not (new.companies & old.companies):
            return "disjoint", []
        return "unknown", []
    new_co = {e for e in new.title_entities if e in COMPANY_TOKENS}
    old_co = {e for e in old.title_entities if e in COMPANY_TOKENS}
    if new_co and old_co:
        return ("shared", sorted(new_co & old_co)) if new_co & old_co else ("disjoint", [])
    return "unknown", []


@dataclass
class NewFacts:
    numbers: list[str] = field(default_factory=list)
    title_terms: list[str] = field(default_factory=list)
    title_entities: list[str] = field(default_factory=list)
    lead_entities: list[str] = field(default_factory=list)
    lead_novelty: float = 0.0  # share of the new lead's content words absent from the earlier article

    @property
    def material(self) -> bool:
        return bool(self.numbers or self.title_terms or self.title_entities)

    def describe(self) -> list[str]:
        out = [f"headline number {n} not in earlier article" for n in self.numbers[:3]]
        out += [f"headline names '{e}' (absent from earlier article)" for e in self.title_entities[:3]]
        out += [f"headline term '{t}' (absent from earlier article)" for t in self.title_terms[:4]]
        if self.lead_entities:
            out.append("lead names " + ", ".join(self.lead_entities[:3]))
        return out


def new_facts(new: StoryFeatures, old: StoryFeatures) -> NewFacts:
    """What `new`'s headline and lead carry that the whole of `old` (headline + article) lacks."""
    old_numbers = numbers_in(old.article.title + " " + old.article.body)
    old_numbers |= {n for n in _PLAIN_NUMBER_RE.findall(old.body_lower)}
    number_pool = title_numbers(new.article.title)
    new_numbers = sorted(n for n in number_pool if n not in old_numbers)

    absent_entities = sorted(
        e for e in new.specific_title_entities if not _mentions(e, old.body_lower)
    )
    lead_entities = sorted(
        e for e in new.lead_entities - new.title_entities
        if e not in COMPANY_TOKENS and not _mentions(e, old.body_lower)
    )
    terms = sorted(
        t for t in new.title_stems
        if t not in old.body_stems and not any(t in e for e in absent_entities)
    )
    lead_stems = content_stems(new.article.lead)
    novelty = (len(lead_stems - old.body_stems) / len(lead_stems)) if lead_stems else 0.0
    return NewFacts(
        lead_novelty=round(novelty, 3),
        numbers=new_numbers,
        title_terms=terms,
        title_entities=absent_entities,
        lead_entities=lead_entities,
    )
