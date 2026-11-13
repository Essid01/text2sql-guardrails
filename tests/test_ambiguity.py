"""Unit tests for glossary-based ambiguity detection."""

import pytest

from text2sql.prompt.ambiguity import (
    apply_clarification,
    detect_ambiguities,
    load_glossary,
)

GLOSSARY = load_glossary()


def _ids(question: str) -> list[str]:
    return [c.term_id for c in detect_ambiguities(question, GLOSSARY)]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What is the revenue per category?", ["revenue_gross_or_net"]),
        ("Quel est le chiffre d'affaires par région ?", ["revenue_gross_or_net"]),
        ("Total turnover of online stores", ["revenue_gross_or_net"]),
        ("Net revenue per category in 2025", []),
        ("Gross revenue per store in 2024", []),
        ("Chiffre d'affaires brut par magasin en 2025", []),
        ("How many orders in March?", ["month_without_year"]),
        ("Combien de commandes en décembre ?", ["month_without_year"]),
        ("How many orders in March 2025?", []),
        ("Number of orders per month in 2025", []),
        ("Who are our best customers?", ["best_customers_metric"]),
        ("Top 10 customers by number of orders", []),
        ("Qui sont nos meilleurs clients ?", ["best_customers_metric"]),
        ("How many customers are in the gold tier?", []),
        ("Number of cancelled orders per store", []),
        ("Revenue in May", ["revenue_gross_or_net", "month_without_year"]),
    ],
)
def test_detection(question: str, expected: list[str]) -> None:
    assert _ids(question) == expected


def test_no_false_positive_on_substrings() -> None:
    assert _ids("Mayor of the city") == []  # "may" only as a whole word
    assert _ids("Which products may need restocking?") == []  # "may" as a verb
    assert _ids("Ventes de chocolat Mars en 2025") == []  # brand, and year given
    assert _ids("Combien de commandes d'avril ?") == ["month_without_year"]
    assert _ids("Network of stores") == []  # "net" inside "network" does not resolve...
    assert _ids("revenue network") == ["revenue_gross_or_net"]  # ...nor count as "net"


@pytest.mark.parametrize("entry", GLOSSARY, ids=lambda e: e.id)
def test_every_option_resolves_its_own_ambiguity(entry) -> None:
    """Invariant: the clarification loop always terminates."""
    trigger_question = {
        "revenue_gross_or_net": "What is the revenue per store?",
        "month_without_year": "How many orders in March?",
        "best_customers_metric": "Who are our best customers?",
    }[entry.id]
    assert entry.is_ambiguous(trigger_question)
    for option in entry.options:
        clarified = apply_clarification(trigger_question, option)
        assert not entry.is_ambiguous(clarified), (entry.id, option.label)


def test_glossary_entries_are_well_formed() -> None:
    assert len({e.id for e in GLOSSARY}) == len(GLOSSARY)
    for entry in GLOSSARY:
        assert entry.triggers and entry.resolved_by
        assert len(entry.options) >= 2
