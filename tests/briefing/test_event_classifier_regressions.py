"""Real sentences from the Ep10-12 validation that the event classifier once got wrong."""
import pytest

from app.content.briefing.events import BACKGROUND, EVENT, EXPLANATION, OTHER, classify

NOT_EVENTS = [
    ("12 points and 3 comments on Hacker News.", OTHER),
    ("Is this the end of the open web?", OTHER),
    ("We chose one-bit precision because it fits on a phone.", OTHER),
    ("My mother won't just lose a soldier, she will lose a son.", OTHER),
    ("If you end early, your partial interview will be deleted.", OTHER),
    ("The certificate is self-signed and cannot be verified.", OTHER),
    ("Bush block in the village cost residents 40 minutes a day.", OTHER),
    ("The startup has raised more than 20 million files for review in Oct.", OTHER),
    ("AI agents used to rely on fixed rules written by engineers.", BACKGROUND),
    ("Capcom has said previously that it will not use AI-generated assets.", BACKGROUND),
    ("For many years, workers used rules to decide who qualified.", BACKGROUND),
    ("Ten years after AlphaGo, researchers are still debating what it showed.", BACKGROUND),
    ("Throughout her career, she has worked with creators and engineers.", BACKGROUND),
    ("Taken together, the data points to a job market finding its footing.", BACKGROUND),
    ("Many are concerned that AI will eventually wipe out jobs.", OTHER),
    ("Admins configure and govern agents centrally from one console.", OTHER),
]

EVENTS = [
    "OpenAI is adding more ads to ChatGPT.",
    "Cohere today unveiled North 2, an overhaul of its platform for running AI agents.",
    "Early tests at Nova Labs showed costs falling by 40 percent, according to the lab.",
    "A report based on a survey of 300 technology executives examines connecting AI agents to enterprise knowledge.",
]


@pytest.mark.parametrize("sentence,expected", NOT_EVENTS)
def test_non_event_sentences_are_never_selected(sentence, expected):
    assert classify(sentence) != EVENT


@pytest.mark.parametrize("sentence", EVENTS)
def test_real_event_sentences_stay_events(sentence):
    assert classify(sentence) == EVENT


def test_explanation_stays_out():
    s = "AI agents need this understanding to reason about situations, make decisions, and ultimately take actions."
    assert classify(s) != EVENT
    assert classify(s) in (EXPLANATION, OTHER)


UNNAMED_SUBJECT = [
    "Early tests showed costs falling by 40 percent, according to the lab.",
    "This week, he resigned from his position and published an essay in The Atlantic.",
    "Researchers released a new dataset of robot grasps.",
]


@pytest.mark.parametrize("sentence", UNNAMED_SUBJECT)
def test_event_needs_a_named_subject(sentence):
    assert classify(sentence) != EVENT


EXPLAINERS = [
    "How to Govern AI Agents",
    "Building a RAG pipeline from scratch",
    "6 Guidelines for Governing AI",
    "What to expect during the AI transition",
    "Prompt caching explained",
]
NEWS_HEADLINES = [
    "OpenAI adds ads to ChatGPT",
    "Nvidia unveils new Blackwell chip",
    "Anthropic raises $2B at a $60B valuation",
]


@pytest.mark.parametrize("headline", EXPLAINERS)
def test_explainer_headlines_are_detected(headline):
    from app.content.briefing.events import is_explainer_headline
    assert is_explainer_headline(headline)


@pytest.mark.parametrize("headline", NEWS_HEADLINES)
def test_news_headlines_are_not_explainers(headline):
    from app.content.briefing.events import is_explainer_headline
    assert not is_explainer_headline(headline)


def test_sentence_that_cannot_be_split_to_fit_the_caption_budget_is_skipped():
    from app.content.briefing.composer import compose
    from app.content.briefing.facts import build_candidates
    headline = "Nova Labs ships Atlas-2"
    long_s = ("Nova Labs released Atlas-2 on Tuesday with a new training recipe that cuts inference "
              "costs by 40 percent for enterprise customers worldwide today.")
    short_s = "Nova Labs said Atlas-2 scored 86.4 percent on the MMLU benchmark."
    comp = compose(headline, build_candidates([long_s, short_s], headline))
    assert comp.body and all("worldwide" not in c.text for c in comp.body)


@pytest.mark.parametrize("headline,expected", [
    ("OpenAI is sticking more ads in ChatGPT", True),
    ("Meta open sources code to let you make Muse AI gadgets", True),
    ("Researchers are tracking a Chinese AI agent fleet", False),
    ("Capcom is preparing for a future where we create games together with AI", False),
    ("How to Govern AI Agents", False),
    ("Open or closed AI? How founders are choosing what to build on", False),
])
def test_headline_is_event_handles_progressive_reports(headline, expected):
    from app.content.briefing.events import headline_is_event
    assert headline_is_event(headline) is expected
