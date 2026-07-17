"""
Generic evaluator for deterministic rules — reads a rule's `formula` and
applies it directly against drawing_data. No new Python function per rule:
adding a deterministic rule is a YAML entry, not a code change, as long as
the field it references already exists in drawing_data.

`formula.type` is a small, closed vocabulary (not a code-eval mini-language)
— every rule stays auditable and there's no injection surface:
  - min_threshold: flag if field < min_value
  - max_threshold: flag if field > max_value
  - exact_match:   flag if field != expected_value
  - formula_ratio: reserved for future use (e.g. pile confinement zone
    length = 3x diameter) — not evaluated in Phase 1; see rules/*.yaml
    comments for why (the real training pile's zone breakdown doesn't yet
    cleanly map to a 2-zone model, so no formula_ratio rule ships yet).
"""
from __future__ import annotations

import logging

from .pathutil import resolve_path
from .schema import Rule

log = logging.getLogger(__name__)

# Same fallback-bbox convention as comparator.py's BBOX_FALLBACK — kept as an
# independent copy (not imported from comparator.py) so this package has no
# dependency on the design-vs-drawing comparator; it's invoked as a sibling
# checker from ed_checker/__init__.py, not from inside comparator.compare().
_DEFAULT_BBOX = {'x': 63, 'y': 22, 'w': 34, 'h': 4}
_NOTES_BBOX = {'x': 63, 'y': 67, 'w': 34, 'h': 9}


def _bbox_for_rule(rule: Rule) -> dict:
    field = (rule.formula or {}).get('field', '')
    if field.startswith('notes.'):
        return dict(_NOTES_BBOX)
    return dict(_DEFAULT_BBOX)


def evaluate_deterministic(rule: Rule, drawing_data: dict) -> dict | None:
    """
    Evaluate one deterministic rule against drawing_data. Returns an issue
    dict (same shape as comparator._issue()'s output — category, title,
    description, suggestion, severity, page_num, x, y, width, height) if the
    rule is violated, or None if it passes or can't be evaluated.

    Callers should have already filtered via retrieval.get_applicable_rules
    so required_entities are known present — this function re-checks the
    formula's own field defensively (a rule can require_entities on a
    different, related field than the one its formula reads) but does not
    re-derive applicability.
    """
    formula = rule.formula or {}
    ftype = formula.get('type')
    if ftype == 'formula_ratio':
        log.info('Rule %s: formula_ratio not yet evaluated (Phase 1 scope) — skipping',
                  rule.rule_id)
        return None

    value = resolve_path(drawing_data, formula['field'])
    if value is None:
        log.debug('Rule %s: field %s not present — skipping', rule.rule_id, formula['field'])
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        # INFO not WARNING: a non-numeric field (e.g. a concrete grade stored as
        # "M40") is a known, permanent limitation for some registered rules — see
        # IS2911-CONCRETE-GRADE-MIN's own comments — not an anomaly. It's
        # retrieved (required_entities presence-check passes on the string) but
        # can never evaluate until a grade-aware formula type exists; logging it
        # at WARNING on every single check would be noise, not signal.
        log.info('Rule %s: field %s = %r is not numeric — skipping',
                 rule.rule_id, formula['field'], value)
        return None

    violated = False
    threshold = None
    if ftype == 'min_threshold':
        threshold = formula['min_value']
        violated = value < threshold
    elif ftype == 'max_threshold':
        threshold = formula['max_value']
        violated = value > threshold
    elif ftype == 'exact_match':
        threshold = formula['expected_value']
        violated = value != threshold
    else:
        log.warning('Rule %s: unrecognised formula.type=%r — skipping', rule.rule_id, ftype)
        return None

    if not violated:
        return None

    def _fmt(n):
        return f'{n:g}'

    message = rule.message_template.format(
        value=_fmt(value),
        min_value=_fmt(threshold) if ftype == 'min_threshold' else None,
        max_value=_fmt(threshold) if ftype == 'max_threshold' else None,
        expected_value=_fmt(threshold) if ftype == 'exact_match' else None,
        unit=rule.unit or '',
    )
    bbox = _bbox_for_rule(rule)
    return {
        'category':    rule.category,
        'title':       f'{rule.title} ({rule.rule_id})',
        'description': message,
        'suggestion':  message,
        'severity':    rule.severity,
        'page_num':    1,
        'x': bbox['x'], 'y': bbox['y'], 'width': bbox['w'], 'height': bbox['h'],
    }


def evaluate_rules(rules: list[Rule], drawing_data: dict) -> list[dict]:
    """Evaluate every rule in `rules` (already retrieval-filtered) and
    return the list of violated-rule issues. See knowledge_rules/__init__.py
    for the one-call public API (load + filter + evaluate)."""
    issues = []
    for rule in rules:
        if rule.rule_type != 'deterministic':
            continue
        issue = evaluate_deterministic(rule, drawing_data)
        if issue:
            issues.append(issue)
    return issues


def build_judgment_issues(findings: list[dict], rules: list[Rule]) -> list[dict]:
    """
    Turn Claude's raw CHECK 7 findings (`[{rule_id, description, bbox}]`, from
    the {KNOWLEDGE_RULES}-injected vision review prompt) into issue dicts,
    using `rules` (the SAME judgment-rule list that was rendered into that
    prompt) to look up each finding's category/title/source_reference —
    keeps prompt content and result interpretation using one consistent rule
    set. A finding citing a rule_id not in `rules` is dropped (Claude
    hallucinated or misquoted an ID) rather than guessed at.
    """
    by_id = {r.rule_id: r for r in rules}
    issues = []
    for finding in findings or []:
        rule = by_id.get(finding.get('rule_id'))
        if not rule:
            log.warning('Judgment finding cites unknown rule_id=%r — dropping',
                        finding.get('rule_id'))
            continue
        description = finding.get('description') or rule.title
        bbox = finding.get('bbox') or _DEFAULT_BBOX
        issues.append({
            'category':    rule.category,
            'title':       f'{rule.title} ({rule.rule_id})',
            'description': f'{description} (Source: {rule.source_reference})',
            'suggestion':  rule.engineering_rationale,
            'severity':    rule.severity,
            'page_num':    1,
            'x': bbox.get('x', _DEFAULT_BBOX['x']),
            'y': bbox.get('y', _DEFAULT_BBOX['y']),
            'width': bbox.get('w', _DEFAULT_BBOX['w']),
            'height': bbox.get('h', _DEFAULT_BBOX['h']),
        })
    return issues
