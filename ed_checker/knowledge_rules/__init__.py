"""
ED Checker knowledge-rule engine — Phase 1.

Turns the reference-document knowledge base (CASAD's design philosophy +
IRC:78/IRC:112/IRC:SP:114/IS 2911/IRC:6) into structured, retrievable,
runtime-evaluated rules instead of one-off hand-written Python checks.

Public API:
    evaluate_all_deterministic(drawing_data, drawing_type) -> list[issue_dict]
        The one-call entry point for deterministic (formula/threshold) rules.
        Called from ed_checker/__init__.py's run_check(), parallel to (not
        inside) comparator.compare() — needs only drawing_data, so it runs
        even with no design Excel uploaded.

    get_applicable_judgment_rules(drawing_type, drawing_data) -> list[Rule]
        Used by pdf_extractor.py to build the {KNOWLEDGE_RULES} block
        injected into the vision review prompt.

Rules are loaded once at import time from knowledge_rules/rules/*.yaml and
cached in _RULES — a malformed rule file fails at import time (server
startup), not silently at check time.
"""
from __future__ import annotations

from .evaluator import build_judgment_issues as _build_judgment_issues
from .evaluator import evaluate_rules
from .retrieval import get_applicable_deterministic_rules, get_applicable_judgment_rules
from .schema import Rule, RuleValidationError, load_rules

_RULES: list[Rule] = load_rules()


def evaluate_all_deterministic(drawing_data: dict, drawing_type: str) -> list[dict]:
    applicable = get_applicable_deterministic_rules(_RULES, drawing_type, drawing_data)
    return evaluate_rules(applicable, drawing_data)


def get_judgment_rules(drawing_type: str, drawing_data: dict) -> list[Rule]:
    return get_applicable_judgment_rules(_RULES, drawing_type, drawing_data)


def build_judgment_issues(findings: list[dict], judgment_rules: list[Rule]) -> list[dict]:
    """Turn Claude's raw CHECK 7 findings into issue dicts. `judgment_rules`
    must be the same list passed to run_review_vision() for the corresponding
    call, so prompt content and result interpretation stay consistent."""
    return _build_judgment_issues(findings, judgment_rules)


__all__ = [
    'evaluate_all_deterministic',
    'get_judgment_rules',
    'build_judgment_issues',
    'Rule',
    'RuleValidationError',
]
