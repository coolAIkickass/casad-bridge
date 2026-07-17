"""
Retrieval layer: given a parsed drawing (drawing_type + drawing_data),
return only the rules that are actually applicable — before any evaluation
happens.

Deliberately NOT semantic/embedding search. Engineering code-compliance
needs guaranteed recall: a rule silently missed by a fuzzy match is a real
defect going unchecked, and there's no way to audit *why* a rule didn't fire
with vector retrieval. This is exact, auditable, structured filtering on two
axes:
  1. applicable_drawing_types — does this rule apply to this kind of drawing?
  2. required_entities — did the parser actually produce every piece of data
     this rule needs to evaluate? If not, the rule is skipped, not guessed
     at — the same "the parser either produced this or it didn't" honesty
     principle already used throughout dxf_extractor.py/comparator.py (e.g.
     geometry_checks scoped to only params Tier-2 routing reliably finds).
"""
from __future__ import annotations

import logging

from .pathutil import resolve_path
from .schema import Rule

log = logging.getLogger(__name__)

# Soft cap on judgment rules injected into one vision prompt call — not a hard
# limit, just a canary so prompt bloat is visible in logs as the rule registry
# grows in future sessions, rather than silently degrading review quality.
_JUDGMENT_RULE_SOFT_CAP = 15


def is_entity_present(drawing_data: dict, path: str) -> bool:
    """
    True if the dotted path resolves to a non-empty value. Empty dict/list/
    string and None all count as "not present" — a rule whose required
    entity is an empty schedule component (e.g. schedule.pilecap == {})
    must not be treated as evaluable.
    """
    value = resolve_path(drawing_data, path)
    if value is None:
        return False
    if isinstance(value, (dict, list, str, set, tuple)) and len(value) == 0:
        return False
    return True


def get_applicable_rules(rules: list[Rule], drawing_type: str, drawing_data: dict) -> list[Rule]:
    """
    Filter the full rule registry down to what's applicable to this specific
    drawing. drawing_type must match DrawingTypeProfile.name (e.g. 'ppp') —
    the same short identifier profiles.py already uses, not the display name
    detect_drawing_type() returns.
    """
    applicable = []
    for rule in rules:
        if drawing_type not in rule.applicable_drawing_types:
            continue
        missing = [p for p in rule.required_entities if not is_entity_present(drawing_data, p)]
        if missing:
            log.debug('Rule %s skipped — missing entities: %s', rule.rule_id, missing)
            continue
        applicable.append(rule)

    log.info('Rule retrieval: %d/%d rules applicable for drawing_type=%r (%s)',
              len(applicable), len(rules), drawing_type,
              [r.rule_id for r in applicable])
    return applicable


def get_applicable_deterministic_rules(rules: list[Rule], drawing_type: str,
                                        drawing_data: dict) -> list[Rule]:
    return [r for r in get_applicable_rules(rules, drawing_type, drawing_data)
            if r.rule_type == 'deterministic']


def get_applicable_judgment_rules(rules: list[Rule], drawing_type: str,
                                   drawing_data: dict) -> list[Rule]:
    judgment = [r for r in get_applicable_rules(rules, drawing_type, drawing_data)
                if r.rule_type == 'judgment']
    if len(judgment) > _JUDGMENT_RULE_SOFT_CAP:
        log.warning(
            '%d judgment rules matched for drawing_type=%r — exceeds soft cap of %d. '
            'Prompt injection will include all of them; consider narrowing '
            'applicable_components on some rules if this grows further.',
            len(judgment), drawing_type, _JUDGMENT_RULE_SOFT_CAP
        )
    return judgment
