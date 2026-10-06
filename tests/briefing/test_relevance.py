"""Source-to-story alignment: does the retrieved text describe what the headline names?
Alignment only -- nothing here judges whether the source is true."""
import json
from datetime import date, datetime

from app.content.briefing.pipeline import Corroborating, compose_briefing
from app.content.briefing.quality import FAIL, PASS, REVIEW, evaluate
from app.content.briefing.relevance import (
    IRRELEVANT, RELEVANT, UNKNOWN, WEAK, assess_relevance, relevance_source_text,
)
from app.models import NewsItem, StoryState
from app.tasks import content_dedup
from tests.briefing import samples as S

NOW = datetime(2026, 9, 22, 12, 0)
TARGET = date(2026, 9, 22)

UNRELATED_README = (
    "# Pixelbrick\n"
    "A command line generator that turns images into brick mosaics. "
    "Install it with pip and run pixelbrick input.png to produce a build guide. "
    "The tool supports custom palettes, adjustable plate sizes and export to PDF. "
    "Contributions are welcome. Please open an issue before sending a pull request. "
    "Licensed under the MIT license."
)

# Same company, different product: the headline is about Atlas-2, the text about a different Nova Labs product.
SAME_COMPANY_OTHER_PRODUCT = (
    "Nova Labs on Tuesday unveiled Beacon, a pair of smart glasses with a built-in camera. "
    "The glasses go on sale next month for 299 dollars in three colours. "
    "Battery life is rated at nine hours of mixed use according to the company. "
    "Nova Labs said it expects to ship 200,000 units before the end of the year. "
    "Reviewers praised the lightweight frame but criticised the companion app."
)

LAWSUIT_HEADLINE = "Publishers sue Nova Labs over training data"
LAWSUIT_ARTICLE = (
    "A group of newspaper publishers filed a lawsuit against Nova Labs on Monday, alleging the company "
    "copied millions of articles to train its models without permission. "
    "The complaint, filed in federal court in New York, seeks damages and an order to delete the data. "
    "Nova Labs said it believes the claims are without merit and will defend itself. "
    "Legal experts say the case could take years to resolve."
)

GEMINI_HEADLINE = "Google launches new reasoning model for developers"
GEMINI_ARTICLE = (
    "Alphabet's Gemini team on Wednesday rolled out a new reasoning model aimed at software engineers. "
    "The model is available through the API starting today and costs 40 percent less than its predecessor. "
    "Developers can use it to plan multi-step coding tasks and review pull requests. "
    "Internal tests show it solves 61 percent of the benchmark problems."
)

SHORT_BUT_RELEVANT = (
    "Nova Labs released Atlas-2 on Tuesday. The open model has 70 billion parameters and an Apache 2.0 license."
)


# ------------------------------------------------------------ the six scenarios

def test_matching_article_is_relevant():
    rel = assess_relevance(S.HEADLINE, S.STRONG_ARTICLE)
    assert rel.status == RELEVANT
    assert {"nova", "labs", "atlas-2"} <= set(rel.matched)
    assert rel.missing_specific == []


def test_unrelated_readme_is_irrelevant():
    rel = assess_relevance(S.HEADLINE, UNRELATED_README)
    assert rel.status == IRRELEVANT
    assert "does not mention" in rel.reason
    assert "atlas-2" in rel.missing


def test_same_company_different_product_is_not_relevant():
    rel = assess_relevance(S.HEADLINE, SAME_COMPANY_OTHER_PRODUCT)
    assert rel.status in (WEAK, IRRELEVANT)
    assert "atlas-2" in rel.missing_specific


def test_title_mismatch_is_irrelevant():
    rel = assess_relevance("Quantum startup Qubitly raises 200 million Series B", S.STRONG_ARTICLE)
    assert rel.status == IRRELEVANT


def test_legitimate_terminology_differences_still_count_as_relevant():
    assert assess_relevance(LAWSUIT_HEADLINE, LAWSUIT_ARTICLE).status == RELEVANT
    # "Google" in the headline, "Alphabet's Gemini" in the text.
    assert assess_relevance(GEMINI_HEADLINE, GEMINI_ARTICLE).status == RELEVANT
    # Hyphenation and stemming: open-weight / open weights, releases / released.
    assert assess_relevance("Nova Labs releases open-weight Atlas-2", "Nova Labs released Atlas2 as open weights on Tuesday. "
                            "It is free to download.").status == RELEVANT


def test_company_alias_is_directional():
    # A Gemini story is not satisfied by a text that only names Google Maps.
    maps_text = "Google Maps added a new transit layer this week. Google said it covers forty cities worldwide."
    assert assess_relevance("Gemini 3 beats rivals on coding benchmarks", maps_text).status != RELEVANT


def test_short_but_clearly_relevant_source_is_relevant():
    rel = assess_relevance(S.HEADLINE, SHORT_BUT_RELEVANT)
    assert rel.status == RELEVANT


# ------------------------------------------------------------------- unknown

def test_unknown_when_source_is_too_short_or_headline_has_no_anchors():
    assert assess_relevance(S.HEADLINE, "Nova Labs released a model.").status == UNKNOWN
    assert assess_relevance(S.HEADLINE, "").status == UNKNOWN
    assert assess_relevance(S.HEADLINE, None).status == UNKNOWN
    assert assess_relevance("New AI update", S.STRONG_ARTICLE).status == UNKNOWN


def test_relevance_is_deterministic():
    first = assess_relevance(S.HEADLINE, UNRELATED_README).as_dict()
    assert all(assess_relevance(S.HEADLINE, UNRELATED_README).as_dict() == first for _ in range(3))


def test_relevance_reads_lines_that_hygiene_would_drop():
    # "Nova Labs (c)" style credit lines are boilerplate for the script but still prove the page's subject.
    raw = "Atlas-2 notes\nNova Labs (c) 2026 Atlas-2 project\nSubscribe to our newsletter for the latest updates."
    text = relevance_source_text("", raw)
    assert "Atlas-2" in text and assess_relevance("Nova Labs Atlas-2 notes", text + " " + "word " * 10).status == RELEVANT


# ------------------------------------------------------------- script gating

def test_irrelevant_source_fails_script_generation():
    result = compose_briefing(S.HEADLINE, "", UNRELATED_README)
    assert result.script_text == ""
    assert result.sufficiency.status == "insufficient"
    assert result.sufficiency.relevance.status == IRRELEVANT
    assert "source does not match the story" in result.generation_reason()
    assert result.quality.status == FAIL


def test_irrelevant_source_is_not_rescued_by_corroboration():
    sibling = Corroborating(source_name="Outlet B", text=S.SIBLING_AGREE)
    result = compose_briefing(S.HEADLINE, "", UNRELATED_README, [sibling])
    assert result.script_text == ""
    assert result.sufficiency.status == "insufficient"


def test_same_company_other_product_does_not_produce_a_script_for_the_wrong_story():
    result = compose_briefing(S.HEADLINE, "", SAME_COMPANY_OTHER_PRODUCT)
    assert result.sufficiency.relevance.status in (WEAK, IRRELEVANT)
    if result.script_text:
        assert result.quality.status != PASS


WEAK_HEADLINE = "Nova Labs Atlas-2 Qubitly Meridian rollout partnership agreement"


def test_weak_relevance_is_capped_at_review():
    # The source is about Nova Labs' Atlas-2 but says nothing of the partnership the headline claims.
    result = compose_briefing(WEAK_HEADLINE, "", S.IDEAL_ARTICLE)
    assert result.sufficiency.relevance.status == WEAK
    assert result.script_text
    assert result.quality.status == REVIEW
    assert any(c["check"] == "source_relevance" and not c["passed"] for c in result.quality.as_dict()["checks"])


def test_a_different_version_of_the_same_product_is_weak_not_relevant():
    rel = assess_relevance("Nova Labs releases Atlas-3 open model", S.IDEAL_ARTICLE)
    assert rel.status == WEAK
    assert rel.missing_specific == ["atlas-3"]


def test_weak_status_from_quality_evaluate_is_review_even_for_a_clean_script():
    good = compose_briefing(S.HEADLINE, "", S.IDEAL_ARTICLE)
    assert good.quality.status == PASS
    weak = assess_relevance(WEAK_HEADLINE, S.IDEAL_ARTICLE)
    assert weak.status == WEAK
    report = evaluate(S.HEADLINE, good.script_text, [S.IDEAL_ARTICLE], "sufficient", weak)
    assert report.status == REVIEW


def test_irrelevant_status_from_quality_evaluate_is_a_hard_fail():
    good = compose_briefing(S.HEADLINE, "", S.STRONG_ARTICLE)
    irrelevant = assess_relevance(S.HEADLINE, UNRELATED_README)
    report = evaluate(S.HEADLINE, good.script_text, [S.STRONG_ARTICLE], "sufficient", irrelevant)
    assert report.status == FAIL
    assert any("does not match the story" in g for g in report.gates)


def test_unknown_relevance_does_not_change_the_outcome():
    good = compose_briefing(S.HEADLINE, "", S.STRONG_ARTICLE)
    unknown = assess_relevance(S.HEADLINE, "")
    assert unknown.status == UNKNOWN
    report = evaluate(S.HEADLINE, good.script_text, [S.STRONG_ARTICLE], "sufficient", unknown)
    assert report.status == good.quality.status


def test_body_that_drifts_off_topic_is_capped_at_review():
    # The headline is satisfied by one line of the source, but the narrated body never mentions it.
    headline = "Nova Labs releases Atlas-2 open model"
    drifting_body = "Acme Gardens launched a watering robot with 12 soil sensors on Monday."
    script = headline + ". " + drifting_body
    source = "Nova Labs Atlas-2 open model notes. " + drifting_body
    report = evaluate(headline, script, [source], "sufficient", assess_relevance(headline, source))
    checks = {c["check"]: c for c in report.as_dict()["checks"]}
    assert checks["body_on_topic"]["passed"] is False
    assert report.status == REVIEW


def test_relevance_is_recorded_in_the_briefing_meta():
    result = compose_briefing(S.HEADLINE, "", S.STRONG_ARTICLE)
    meta = json.loads(json.dumps(result.meta()))
    assert meta["sufficiency"]["relevance"]["status"] == RELEVANT
    assert meta["sufficiency"]["relevance"]["matched"]


# ------------------------------------------------- stored status / ranking input

def _story(db, title, summary, raw_content=None):
    item = NewsItem(
        title=title, canonical_url=f"https://example.test/{title.replace(' ', '-')}",
        source_name="Example Source", source_type="rss", published_at=NOW, collected_at=NOW,
        collection_date=TARGET, status="collected", raw_summary=summary, raw_content=raw_content,
    )
    db.add(item)
    db.flush()
    state = StoryState(id=item.id, ai_relevance="ai_candidate", ai_relevance_score=0.8)
    db.add(state)
    db.commit()
    return item, state


def test_irrelevant_story_is_stored_as_insufficient_with_the_relevance_detail(db_session):
    item, state = _story(db_session, S.HEADLINE, "", UNRELATED_README)
    assert content_dedup.assess_source_sufficiency(db_session, item, state) == "insufficient"
    detail = json.loads(state.sufficiency_detail)
    assert detail["relevance"]["status"] == IRRELEVANT
    assert "source does not match the story" in detail["reason"]
