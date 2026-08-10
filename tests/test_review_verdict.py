"""Tests for parsing the UNFIXABLE-review agent's verdict.

Regression: the verdict was detected with `"REJECT" in result.upper()`,
so an ACCEPT verdict whose prose contained the word "reject" ("I would
not reject this lightly…") flipped the decision.
"""
from __future__ import annotations

import pytest

from pr_manager.agent import _parse_review_verdict


def test_plain_accept():
    assert _parse_review_verdict("ACCEPT") == ("accept", "")


def test_plain_reject_with_feedback():
    decision, feedback = _parse_review_verdict("REJECT: the PR exists to fix this")
    assert decision == "reject"
    assert feedback == "the PR exists to fix this"


def test_reject_after_analysis_lines():
    decision, feedback = _parse_review_verdict(
        "The PR title says it fixes CI.\nREJECT: fixing CI is the whole point"
    )
    assert decision == "reject"
    assert feedback == "fixing CI is the whole point"


def test_reject_feedback_spans_following_lines():
    decision, feedback = _parse_review_verdict(
        "REJECT: two reasons.\nFirst, X.\nSecond, Y."
    )
    assert decision == "reject"
    assert "First, X." in feedback
    assert "Second, Y." in feedback


def test_accept_mentioning_reject_in_prose_is_not_a_reject():
    decision, _feedback = _parse_review_verdict(
        "I would not reject this claim lightly, but it holds up.\nACCEPT"
    )
    assert decision == "accept"


def test_unparseable_output_defaults_to_accept():
    decision, _feedback = _parse_review_verdict("no verdict here at all")
    assert decision == "accept"
