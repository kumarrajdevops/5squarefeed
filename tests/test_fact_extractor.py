from app.extraction.fact_extractor import (
    extract_companies,
    extract_dates,
    extract_events,
    extract_facts,
    extract_numeric_claims,
    extract_products,
)


def test_extract_companies_finds_known_names():
    companies = extract_companies("OpenAI and NVIDIA announced a partnership today.")
    assert "openai" in companies
    assert "nvidia" in companies


def test_extract_companies_avoids_substring_false_positives():
    # "meta" must match as a whole word, not as a substring of an
    # unrelated word -- same word-boundary discipline as
    # app/filters/ai_relevance.py's find_matches().
    companies = extract_companies("The metadata was updated yesterday.")
    assert "meta" not in companies


def test_extract_products_finds_known_model_names():
    products = extract_products("GPT-5 and Claude both scored well on the benchmark.")
    assert "gpt-5" in products
    assert "claude" in products


def test_extract_products_returns_empty_when_none_found():
    assert extract_products("A story about something else entirely.") == []


def test_extract_events_matches_launch_category():
    events = extract_events("The company launches its new product today.")
    assert "launch" in events


def test_extract_events_matches_multiple_categories():
    events = extract_events(
        "After raising a new funding round, the startup was acquired for $2 billion."
    )
    assert "funding" in events
    assert "acquisition" in events


def test_extract_events_returns_empty_for_neutral_text():
    assert extract_events("The weather was pleasant this weekend.") == []


def test_extract_events_does_not_false_positive_on_fine_tuning():
    # Real bug, caught live during the taxonomy redesign session: bare
    # "fine" used to be in lawsuit_regulatory's keyword set, and
    # \bfine\b matches inside "fine-tuning"/"fine-tuned" since regex
    # treats the hyphen as a word boundary -- a story about fine-tuning
    # a model has nothing to do with a regulatory fine. "fined" alone
    # (no hyphen collision) still correctly matches.
    events = extract_events("Fyxer uses fine-tuning and real user feedback to organize inboxes.")
    assert "lawsuit_regulatory" not in events

    events = extract_events("The company was fined $2 million by regulators.")
    assert "lawsuit_regulatory" in events


def test_extract_dates_finds_month_name_date():
    dates = extract_dates("The event is scheduled for March 15, 2026.")
    assert any("March 15, 2026" in d for d in dates)


def test_extract_dates_finds_iso_date():
    dates = extract_dates("Released on 2026-03-15 according to the filing.")
    assert "2026-03-15" in dates


def test_extract_dates_returns_empty_when_no_date_present():
    assert extract_dates("No date mentioned in this sentence at all.") == []


def test_extract_numeric_claims_finds_dollar_amount():
    claims = extract_numeric_claims("The deal was valued at $300 million.")
    assert any("$300" in c for c in claims)


def test_extract_numeric_claims_finds_percentage():
    claims = extract_numeric_claims("Performance improved by 90% after the update.")
    assert "90%" in claims


def test_extract_numeric_claims_finds_multiplier():
    claims = extract_numeric_claims("The new model is 10x faster than before.")
    assert "10x" in claims


def test_extract_numeric_claims_returns_empty_for_no_numbers():
    assert extract_numeric_claims("A qualitative story with no figures.") == []


def test_extract_facts_combines_all_categories():
    facts = extract_facts(
        title="OpenAI launches GPT-5 after raising $300 million",
        summary="The launch happened on March 15, 2026 and performance rose 90%.",
    )
    assert facts["companies"] == ["openai"]
    assert facts["products"] == ["gpt-5"]
    assert "launch" in facts["events"]
    assert "funding" in facts["events"]
    assert any("March 15, 2026" in d for d in facts["dates"])
    assert "90%" in facts["claims"]
    assert any("$300" in c for c in facts["claims"])


def test_extract_facts_handles_no_summary_without_crashing():
    facts = extract_facts(title="A plain headline with no AI terms", summary=None)
    assert facts == {
        "companies": [],
        "products": [],
        "events": [],
        "dates": [],
        "claims": [],
    }
