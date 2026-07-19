"""
Engineering Reasoning Reviewer — the "AI Reasoning" tab's backend.

Public API:
  get_relevant_concepts(drawing_type, drawing_data) -> list[Concept]
  build_structured_summary(drawing_data, issues) -> str
  build_reasoning_issues(review_data) -> list[dict]   (_issue()-shaped, category='AI Reasoning')

Deliberately separate from ed_checker/knowledge_rules/ — see that package's
own module docstrings for why: rules are individually evaluated; concepts
here are retrieved as context for one holistic synthesis call and never
evaluated on their own. See CLAUDE.md's Engineering Reasoning Reviewer
section (once added) for the full architecture.
"""
from __future__ import annotations

import logging

from .retrieval import get_relevant_concepts as _get_relevant_concepts
from .schema import Concept, ConceptValidationError, load_concepts
from .summary import build_structured_summary

log = logging.getLogger(__name__)

_CONCEPTS: list[Concept] = load_concepts()

_VALID_CONFIDENCE = ('definite', 'probable', 'needs_verification')
_DEFAULT_BBOX = {'x': 63, 'y': 22, 'w': 34, 'h': 4}


def get_relevant_concepts(drawing_type: str, drawing_data: dict) -> list[Concept]:
    return _get_relevant_concepts(_CONCEPTS, drawing_type, drawing_data)


def build_reasoning_issues(review_data: dict | None) -> list[dict]:
    """
    Turn the reviewer's raw `reasoning_findings` (from run_engineering_review)
    into _issue()-shaped dicts. severity is always 'error' (cosmetically
    unused for this category, kept only for DB NOT NULL / schema
    consistency — the review UI renders this category in its own "AI
    Reasoning" tab, not the severity-driven Issues list). confidence is the
    field that actually distinguishes these findings for the engineer.
    """
    if not review_data:
        return []
    findings = review_data.get('reasoning_findings') or []
    issues = []
    for finding in findings:
        confidence = finding.get('confidence')
        if confidence not in _VALID_CONFIDENCE:
            log.warning('Reasoning finding has invalid/missing confidence=%r — defaulting to '
                        'needs_verification (most conservative)', confidence)
            confidence = 'needs_verification'
        bbox = finding.get('bbox') or _DEFAULT_BBOX
        issues.append({
            'category':    'AI Reasoning',
            'title':       finding.get('title') or 'Engineering observation',
            'description': finding.get('description') or '',
            'suggestion':  '',
            'severity':    'error',
            'confidence':  confidence,
            'page_num':    1,
            'x': bbox.get('x', _DEFAULT_BBOX['x']),
            'y': bbox.get('y', _DEFAULT_BBOX['y']),
            'width':  bbox.get('w', _DEFAULT_BBOX['w']),
            'height': bbox.get('h', _DEFAULT_BBOX['h']),
        })
    return issues


__all__ = [
    'get_relevant_concepts', 'build_structured_summary', 'build_reasoning_issues',
    'Concept', 'ConceptValidationError',
]
