"""Is a source sentence a news EVENT, or explanation/background/significance/motive?

The briefing narrates what happened, not why it matters. A sentence is selected
only when it reports a concrete development (announcement, launch, release,
result, report, finding, statement, action, change). Explanation, background,
significance and unattributed motive are never selected, however informative.

Pure, regex based and deterministic. The tables are deliberately small and
readable so they can be tuned against real misclassifications."""
import re

EVENT = "event"
EXPLANATION = "explanation"
BACKGROUND = "background"
SIGNIFICANCE = "significance"
INTENT = "intent"
OTHER = "other"

_QUOTED_RE = re.compile(r"[“\"][^”\"]*[”\"]")

# Concrete news actions. Only verb forms that cannot be nouns ("files", "block", "cost",
# "test", "rank", "offer", "update" are all excluded in their bare/plural forms): past
# tense, headline-style present, and auxiliary constructions. Trailing word boundaries matter:
# "invest" must not match "investors".
_ACTION_RE = re.compile(
    r"(?<![\w-])(?:"
    r"announc(?:e|es|ed|ing)|launch(?:es|ed|ing)|releas(?:e|es|ed|ing)|introduc(?:e|es|ed|ing)|unveil(?:s|ed|ing)?|"
    r"acqui(?:re|res|red|ring)|acquisition|rais(?:es|ed|ing)|cut|cuts|ban(?:s|ned|ning)|sue(?:s|d)|suing|"
    r"fil(?:ed|ing)|quit|quits|resign(?:s|ed|ing)?|fire(?:s|d)|hire(?:s|d)|laid off|lay(?:s|ing)? off|"
    r"find(?:s|ing|ings)|found|reveal(?:s|ed|ing)?|report(?:ed|edly)|publish(?:es|ed|ing)|opened|"
    r"open-sourc(?:e|es|ed|ing)|updated|partner(?:s|ed|ing)|shipped|ships|reached|reaches|won|wins|lost|loses|dropped|"
    r"rose|grew|surpass(?:es|ed|ing)|beat|beats|scor(?:es|ed)|approv(?:es|ed|al)|reject(?:s|ed)|blocked|"
    r"warn(?:s|ed)|admit(?:s|ted)|confirm(?:s|ed)|den(?:ies|ied)|agree(?:s|d)|signed|settle(?:s|d)|"
    r"invest(?:s|ed|ing)|funded|bought|sold|paid|charged|offered|added|adds|remov(?:es|ed)|replac(?:es|ed)|"
    r"expand(?:s|ed)|cancel(?:s|led|ed)|delay(?:s|ed)|tested|discover(?:s|ed)|develop(?:s|ed)|deploy(?:s|ed)|"
    r"adopt(?:s|ed)|accus(?:es|ed)|alleg(?:es|ed|edly)|claimed|said|says|told|tells|appoint(?:s|ed)|"
    r"joined|joins|began|begins|started|examin(?:es|ed)|surveyed|studied|ranked|debut(?:s|ed)|"
    r"roll(?:s|ed|ing)? out|leaked|hacked|breach(?:ed)?|detected|trained|built|upgrad(?:es|ed)|rebrand(?:s|ed)|"
    r"merg(?:es|ed)|showed|shows|demonstrat(?:es|ed)|conclud(?:es|ed)|identified|measured|"
    r"will (?:host|release|launch|ship|open|begin|roll|offer|start|include|add|cost|charge|unveil|introduce|"
    r"be (?:available|released|launched|offered|shut|retired|discontinued))|"
    r"(?:is|are|was|were) (?:available|releasing|launching|rolling|adding|cutting|shutting|testing|"
    r"expanding|ending|closing|opening|raising|buying|selling|investing|hiring|firing|suing|"
    r"acquiring|banning|rejecting|approving|delaying|cancelling|canceling|replacing|removing)|"
    r"(?:has|have|had) (?:been )?(?:released|launched|added|acquired|raised|opened|banned|cut|approved|published|"
    r"agreed|signed|announced|introduced|unveiled|started|begun|filed|sued|appointed|hired|fired|rejected|"
    r"blocked|shut|retired|discontinued|rolled out|gone|quit|resigned)|"
    r"available (?:under|on|from|in|to|now|today)"
    r")\b",
    re.I,
)

# Not events whatever verbs they contain.
_HN_STATS_RE = re.compile(r"^\d[\d,]*\s+points?\b|\bcomments? on (?:Hacker News|HN)\b", re.I)
_QUESTION_RE = re.compile(r"\?\s*[”\"’']?$")
_FIRST_PERSON_RE = re.compile(r"\b(?:I|I'm|I've|I'd|I'll|My|my|me|We|we|We're|we're|We've|we've|Our|our|ours|Us|us|Let's|let's)\b")
_FRAGMENT_END_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|Inc|Ltd|Corp|Co|Dr|Mr|Mrs|Ms|St|vs|No|approx)\.$"
)
_NUMBER_FRAGMENT_RE = re.compile(r"^\d[\d,.]*\s+(?:followed|and|to|through|at|with|by|from|of|or|in)\b")
_PURPOSE_RE = re.compile(
    r"\b(?:the (?:purpose|goal|aim|point|idea|mission) of|is threefold|put simply|in other words|"
    r"to put it simply|what (?:does|is|are)\b)",
    re.I,
)
_SECOND_PERSON_RE = re.compile(r"^(?:if|when|once|should) you\b|\byou(?:r|rs|rself)?\b", re.I)
_OPINION_RE = re.compile(r"\b(?:are|is|were|was) (?:concerned|worried|afraid|convinced)|\b(?:fears?|worr(?:y|ies)) that\b", re.I)
_PREVIOUSLY_RE = re.compile(r"\b(?:previously|has long|have long|in the past|so far this year)\b", re.I)
_USED_TO_RE = re.compile(r"\bused to\b", re.I)
_PAST_YEAR_RE = re.compile(r"\b(?:19[5-9]\d|20[01]\d|202[0-5])\b")
_ELAPSED_RE = re.compile(
    r"\b(?:years? (?:after|before|ago|earlier|later)|for (?:many|several|a few|\d+) (?:years|decades)|decades? (?:ago|earlier)|in the (?:early|late|mid) (?:19|20)\d0s)\b", re.I)
_EXPLAIN_START_RE = re.compile(r"^(?:for (?:reference|example|instance)|note that|recall that|remember that|in (?:practice|theory|general|short|essence))\b", re.I)
_QUOTE_WORDS_RE = re.compile(r"[“\"][^”\"]+[”\"]")
_QUOTE_OPENERS = "“\"‘'"

_PUBLICATION_SUBJECT_RE = re.compile(
    r"\b(?:report|study|survey|paper|analysis|research|poll|whitepaper|white paper|ranking|index|audit|"
    r"investigation|filing|lawsuit|complaint|ruling|bill|law|regulation|memo|letter)\b",
    re.I,
)

# "The report examines ...", "The study found ..." -- a publication doing something.
_PUBLICATION_VERB_RE = re.compile(
    r"\b(?:examin\w*|find(?:s|ing|ings)?|found|show(?:s|ed)?|reveal\w*|conclud\w*|measur\w*|analy[sz]\w*|"
    r"explor\w*|look(?:s|ed)? at|assess\w*|compar\w*|document\w*|estimat\w*|identif\w*|highlight\w*|"
    r"detail\w*|cover(?:s|ed)?|surveyed|based on)\b",
    re.I,
)

# Significance / opinion / forecast. Scanned outside quotes: a quoted statement is
# an attributed *statement* (an event), the quote's contents are not ours to judge.
_SIGNIFICANCE_RE = re.compile(
    r"\b(?:means that|meaning that|could|might|may (?:be|have|lead|help|force|make|become|change)|implications?|"
    r"raises? (?:concerns?|questions?)|marks the|marking the|signals? (?:a|that|the)|reshap\w*|transform\w*|"
    r"paves? the way|game[- ]chang\w*|important|matters?|critical(?:ly)?|threat(?:en|ens)?|poses? a|"
    r"expected to (?:change|reshape|transform|disrupt)|set to (?:change|reshape|transform|disrupt)|"
    r"promises? to|bodes?|underscores?|highlights? (?:the|how|why)|shows? (?:how|why) )\b",
    re.I,
)

# Motive / intent. "OpenAI is adding more ads" is the event; "OpenAI wants to
# monetize ChatGPT" is a motive nobody stated. Allowed only when attributed.
_INTENT_RE = re.compile(
    r"\b(?:wants?|wanted|aims?|aimed|hopes?|hoped|seeks?|sought|intends?|intended|looks? to|looking to|"
    r"trying to|tries to|in an effort to|in order to|so that|so as to|as it (?:seeks|looks|tries|moves)|"
    r"eager to|bid to|push to|race to|to (?:monetize|monetise|boost revenue|capture|cash in|win over|keep up|"
    r"stay ahead|catch up|compete))\b",
    re.I,
)
_ATTRIBUTION_RE = re.compile(
    r"\b(?:said|says|told|tells|according to|announced|stated|states|confirmed|wrote|writes|acknowledged|"
    r"explained|explains|argued|argues|added|noted|reported|reports|claimed|claims)\b",
    re.I,
)

_BACKGROUND_START_RE = re.compile(
    r"^(?:previously|formerly|originally|historically|traditionally|in \d{4}\b|since \d{4}\b|last year|"
    r"for years|for decades|over the (?:years|past)|in recent years|in the past|years ago|ever since|"
    r"founded (?:in|by)|launched in \d{4}|the company (?:was )?founded|it was founded|"
    r"throughout (?:his|her|their|its|the)|in (?:his|her|their|its) (?:day|career|role|time)|writ large|"
    r"on the (?:whole|other hand)|more broadly|broadly speaking|at (?:its|the) core|early on|at the time|back then|"
    r"before (?:the|he|she|they|it|joining|launching)|taken together|all told|overall,|in sum|in summary|in conclusion)",
    re.I,
)
# Inline background markers do not exclude a sentence outright (a result sentence can
# mention "last year's model"); they only cost it score.
BACKGROUND_INLINE_RE = re.compile(
    r"\b(?:last year|years ago|previously|formerly|founded in|known for|best known|has long|have long|"
    r"has always|in recent years|over the years|historically)\b",
    re.I,
)

# A generic, non-specific subject doing a generic thing: how-it-works, definitions,
# purpose. Never news.
_GENERIC_SUBJECT_RE = re.compile(
    r"^(?:(?:ai|a\.i\.|artificial intelligence|machine learning|generative ai|ai agents?|agents?|"
    r"large language models?|llms?|models?|companies|organi[sz]ations|developers|users|businesses|"
    r"enterprises|data|knowledge|context|understanding|tools?|systems?|technology|technologies|software|"
    r"teams|engineers|researchers|people|humans|workers|leaders|customers|most|many|some|every|each|any)\b"
    r"(?:\s+\w+){0,3}?\s+(?:need|needs|must|should|can|cannot|may|often|typically|usually|require|requires|"
    r"rely|relies|have to|has to|tend to|tends to|are|is|use|uses|work|works|learn|learns|help|helps|"
    r"allow|allows|enable|enables)\b)",
    re.I,
)
_DEFINITION_RE = re.compile(
    r"^(?:the |a |an )?[A-Z][\w\-]*(?:\s+[\w\-]+){0,3}\s+(?:is|are)\s+(?:a|an|the)\s+", re.I
)
_HOW_IT_WORKS_RE = re.compile(
    r"\b(?:works? by|allows? (?:users|developers|teams|people|you) to|helps? (?:users|developers|teams|people) |"
    r"designed to|used to|is used for|are used for|lets (?:users|developers|you)|"
    r"(?:in|so) order to|to (?:reason|understand|make decisions|take actions?))\b",
    re.I,
)

_GENERIC_START_WORDS = frozenset(
    "ai a.i. artificial machine generative agents agent large models model companies organizations "
    "organisations developers users businesses enterprises data knowledge context understanding tools tool "
    "systems system technology technologies software teams engineers researchers people humans workers "
    "leaders customers most many some every each any".split()
)


def _unquoted(sentence: str) -> str:
    return _QUOTED_RE.sub(" ", sentence)


def is_attributed(sentence: str) -> bool:
    return bool(_ATTRIBUTION_RE.search(sentence))


def has_action(sentence: str) -> bool:
    text = _unquoted(sentence)
    if _ACTION_RE.search(text):
        return True
    return bool(_PUBLICATION_SUBJECT_RE.search(text) and _PUBLICATION_VERB_RE.search(text))


def has_significance(sentence: str) -> bool:
    return bool(_SIGNIFICANCE_RE.search(_unquoted(sentence)))


def has_unattributed_intent(sentence: str) -> bool:
    text = _unquoted(sentence)
    return bool(_INTENT_RE.search(text)) and not is_attributed(sentence)


def has_background_inline(sentence: str) -> bool:
    return bool(BACKGROUND_INLINE_RE.search(sentence))


def _generic_subject(sentence: str) -> bool:
    first = sentence.split(None, 1)[0].lower().strip("“\"‘'") if sentence.split() else ""
    return first in _GENERIC_START_WORDS


_SUBJECT_STOPWORDS = {
    "the", "a", "an", "this", "that", "these", "those", "in", "on", "at", "after", "before", "many", "some", "most",
    "several", "early", "recent", "last", "over", "under", "today", "yesterday", "now", "then", "here", "there",
    "meanwhile", "however", "but", "and", "so", "as", "when", "while", "with", "for", "by", "from", "if", "all",
    "both", "each", "every", "new", "one", "two", "three", "ai", "it", "he", "she", "they", "we", "i", "you",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march",
    "april", "may", "june", "july", "august", "september", "october", "november", "december", "sources", "people",
    "researchers", "engineers", "users", "developers", "experts", "analysts", "officials", "scientists", "admins",
}
_CAMEL_RE = re.compile(r"[a-z]+[A-Z]")


def has_named_subject(sentence: str) -> bool:
    """Does the part of the sentence before its action verb name someone or something
    specific (a company, person, product, model, body)? 'Early tests showed ...' and
    'Developers configure ...' name nobody, so they are not selected as the event."""
    text = _unquoted(sentence)
    m = _ACTION_RE.search(text)
    if m is None:
        return True  # publication path ("A report ... examines ..."): subject is the document
    head = text[: m.start()]
    if _PUBLICATION_SUBJECT_RE.search(head):
        return True  # "A report based on a survey of ... examines ..."
    for index, word in enumerate(head.split()):
        clean = word.strip(".,;:!?()[]“”\"'’‘—-")
        if len(clean) < 2 or clean.lower() in _SUBJECT_STOPWORDS:
            continue
        if clean[0].isupper() or _CAMEL_RE.match(clean) or (clean[0].isdigit() and any(ch.isalpha() for ch in clean)):
            return True
    return False


def classify(sentence: str) -> str:
    """EVENT only for a concrete development; everything else names why it is not."""
    text = (sentence or "").strip()
    if not text:
        return OTHER
    if _HN_STATS_RE.search(text) or _QUESTION_RE.search(text) or _FRAGMENT_END_RE.search(text) or _NUMBER_FRAGMENT_RE.match(text):
        return OTHER
    if text[0] in _QUOTE_OPENERS:
        return OTHER  # a quoted opinion, not a reported development
    if _FIRST_PERSON_RE.search(_unquoted(text)):
        return OTHER
    unq = _unquoted(text)
    if _SECOND_PERSON_RE.search(unq) or _OPINION_RE.search(unq):
        return OTHER
    if _QUOTE_WORDS_RE.search(text) and len(unq.split()) < 0.5 * len(text.split()):
        return OTHER  # mostly a quotation: an opinion, not a development
    if _BACKGROUND_START_RE.match(text) or _USED_TO_RE.search(unq) or _PREVIOUSLY_RE.search(unq) or _PAST_YEAR_RE.search(unq) or _ELAPSED_RE.search(unq):
        return BACKGROUND
    if _EXPLAIN_START_RE.match(text):
        return EXPLANATION
    if _PURPOSE_RE.search(_unquoted(text)):
        return EXPLANATION
    if has_significance(text):
        return SIGNIFICANCE
    if has_unattributed_intent(text):
        return INTENT
    unquoted = _unquoted(text)
    if _GENERIC_SUBJECT_RE.match(unquoted.strip()) and not is_attributed(text):
        return EXPLANATION
    if _HOW_IT_WORKS_RE.search(unquoted) and not has_strong_event(unquoted):
        return EXPLANATION
    if _DEFINITION_RE.match(unquoted.strip()) and not has_strong_event(unquoted):
        return EXPLANATION
    if has_action(text) and has_named_subject(text):
        return EVENT
    return OTHER


def has_strong_event(sentence: str) -> bool:
    """A concrete action verb, ignoring the weak purpose/describing phrases above."""
    return bool(_ACTION_RE.search(_unquoted(sentence)))


def is_event(sentence: str) -> bool:
    return classify(sentence) == EVENT


_EXPLAINER_HEADLINE_RE = re.compile(
    r"^(?:how (?:to|do|does|can|we|i)|why |what (?:is|are|to|you|we)|when to|where to|a (?:beginner|practical|complete|quick)|"
    r"(?:a |the )?(?:guide|introduction|intro|primer|intro) to|building (?:a|an|your)|understanding|getting started|"
    r"\d+ (?:guidelines|tips|ways|things|lessons|reasons|steps|rules|principles|mistakes)|"
    r"(?:\w+ ){0,3}(?:tutorial|explained|walkthrough|cheat ?sheet|best practices))"
    r"|\b(?:tutorial|walkthrough|cheat ?sheet)\b|\bexplained[.!]?$|:\s*(?:a|the) (?:guide|tutorial|primer|introduction)",
    re.I,
)


def is_explainer_headline(headline: str) -> bool:
    """How-to, guide, tutorial, listicle and 'what to expect' headlines are not news events."""
    return bool(_EXPLAINER_HEADLINE_RE.search((headline or "").strip()))


_HEADLINE_PROGRESSIVE_RE = re.compile(
    r"\b(?:is|are)\s+(?:(?:now|also|quietly|already|finally)\s+)?"
    r"(?!(?:prepar|plann|look|hop|seek|aim|work|try|consider|think|want|expect)ing\b)(?:\w+ing)\b|open[- ]sources?\b",
    re.I,
)


def headline_is_event(headline: str) -> bool:
    """Does the headline itself state a development (and not only a topic)?"""
    text = (headline or "").strip()
    if not text or _generic_subject(text):
        return False
    if "?" in text or has_significance(text) or is_explainer_headline(text):
        return False
    return has_action(text) or (bool(_HEADLINE_PROGRESSIVE_RE.search(text)) and has_named_subject(text))
