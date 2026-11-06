"""Merging near-duplicate supporting quotes: a quote contained in another quote is dropped."""
import copy
import json

import pytest

from access_intake.validate import merge_quotes, validate_extraction
from conftest import ALL_DOCUMENT_IDS, MOCK_RESPONSES

FIXTURES = json.loads(MOCK_RESPONSES.read_text(encoding="utf-8"))["documents"]


@pytest.mark.parametrize("quotes, merged", [
    (["My manager is Priya Shah", "My manager is Priya Shah."], ["My manager is Priya Shah."]),
    (["a b c.", "x", "a b c", "a b"], ["a b c.", "x"]),
    (["x", "a b", "a b c."], ["x", "a b c."]),           # the longer quote takes the earliest position of its group
    (["same", "same"], ["same"]),
    (["from 2026-10-23", "to 2026-11-06"], ["from 2026-10-23", "to 2026-11-06"]),   # overlap is not containment
    ([], []),
])
def test_merge_quotes(quotes, merged):
    assert merge_quotes(quotes) == merged
    assert merge_quotes(merged) == merged                  # idempotent


def test_live_style_quotes_merge_with_the_sentences_code_adds(load, schema):
    """IT-020 as Llama returned it: short quotes without the full stop, plus the conflict check's sentences."""
    extraction = copy.deepcopy(FIXTURES["IT-020.json"]["extraction"])
    extraction.update(manager="Elena Park", issues=[], supporting_quotes=[
        "I need new Atlas read access to staging from 2026-10-29 to 2026-11-02",
        "My manager is Priya Shah", "The manager for this request is Elena Park"])
    valid = validate_extraction(extraction, load("IT-020.json"), schema)
    assert valid.extraction["supporting_quotes"] == [
        "I need new Atlas read access to staging from 2026-10-29 to 2026-11-02",     # kept as the model gave it
        "My manager is Priya Shah.", "The manager for this request is Elena Park."]  # full sentences, in place
    assert set(valid.merged_quotes) == {"My manager is Priya Shah", "The manager for this request is Elena Park"}
    message = load("IT-020.json").message
    assert all(q in message for q in valid.extraction["supporting_quotes"])


@pytest.mark.parametrize("document_id", ALL_DOCUMENT_IDS)
def test_fixture_quotes_are_unchanged(document_id, load, schema):
    valid = validate_extraction(FIXTURES[document_id]["extraction"], load(document_id), schema)
    assert valid.merged_quotes == ()
    assert valid.extraction["supporting_quotes"] == FIXTURES[document_id]["extraction"]["supporting_quotes"]


def test_a_model_quote_inside_another_model_quote_is_dropped(load, schema):
    extraction = copy.deepcopy(FIXTURES["IT-001.json"]["extraction"])
    extraction["supporting_quotes"].append("read access on Atlas")
    valid = validate_extraction(extraction, load("IT-001.json"), schema)
    assert valid.merged_quotes == ("read access on Atlas",)
    assert valid.extraction["supporting_quotes"] == FIXTURES["IT-001.json"]["extraction"]["supporting_quotes"]
