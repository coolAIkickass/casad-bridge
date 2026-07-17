"""
Rule schema + YAML loader for the ED Checker knowledge-rule engine.

A "rule" is one deterministic or judgment-based check derived from an
engineering reference document (CASAD's own design philosophy, or an IRC/IS
code), stored as structured YAML rather than hand-written Python. See
`ed_checker/knowledge_rules/rules/*.yaml` for the actual rule content and
`retrieval.py`/`evaluator.py` for how rules get selected and run.

Validation is intentionally strict and fails at load time (not silently at
evaluation time) — a malformed rule is a content bug, not a runtime
condition, and should be caught before it ever reaches a real drawing.
"""
from __future__ import annotations

import glob
import logging
import os
from dataclasses import dataclass, field

import yaml

log = logging.getLogger(__name__)

RULE_TYPES = ('deterministic', 'judgment')
VALIDATION_STATUSES = ('implemented', 'pending_pilot_validation', 'needs_real_example')
FORMULA_TYPES = ('min_threshold', 'max_threshold', 'exact_match', 'formula_ratio')

_REQUIRED_FIELDS = (
    'rule_id', 'title', 'applicable_drawing_types', 'required_entities',
    'rule_type', 'category', 'engineering_rationale', 'source_reference',
)


class RuleValidationError(ValueError):
    """A rule YAML entry is malformed — fails loudly at load time."""


@dataclass(frozen=True)
class Rule:
    rule_id: str
    title: str
    applicable_drawing_types: tuple[str, ...]
    required_entities: tuple[str, ...]
    rule_type: str                          # 'deterministic' | 'judgment'
    category: str
    engineering_rationale: str
    source_reference: str
    applicable_components: tuple[str, ...] = field(default_factory=tuple)
    formula: dict | None = None             # deterministic only
    reasoning_prompt: str | None = None      # judgment only
    unit: str | None = None
    severity: str = 'error'
    message_template: str | None = None     # deterministic only
    validation_status: str = 'pending_pilot_validation'
    source_file: str = ''                   # populated by the loader, not user-supplied


def _validate_raw(raw: dict, source_file: str) -> None:
    missing = [f for f in _REQUIRED_FIELDS if not raw.get(f)]
    if missing:
        raise RuleValidationError(
            f'{source_file}: rule {raw.get("rule_id", "<no rule_id>")!r} missing required '
            f'field(s): {missing}'
        )
    if raw['rule_type'] not in RULE_TYPES:
        raise RuleValidationError(
            f'{source_file}: rule {raw["rule_id"]!r} has rule_type={raw["rule_type"]!r}, '
            f'must be one of {RULE_TYPES}'
        )
    if raw['rule_type'] == 'deterministic':
        formula = raw.get('formula')
        if not formula or not isinstance(formula, dict):
            raise RuleValidationError(
                f'{source_file}: rule {raw["rule_id"]!r} is deterministic but has no formula'
            )
        if formula.get('type') not in FORMULA_TYPES:
            raise RuleValidationError(
                f'{source_file}: rule {raw["rule_id"]!r} formula.type={formula.get("type")!r}, '
                f'must be one of {FORMULA_TYPES}'
            )
        if not formula.get('field'):
            raise RuleValidationError(
                f'{source_file}: rule {raw["rule_id"]!r} formula missing "field" '
                f'(dotted path into drawing_data)'
            )
        if not raw.get('message_template'):
            raise RuleValidationError(
                f'{source_file}: rule {raw["rule_id"]!r} is deterministic but has no '
                f'message_template'
            )
    if raw['rule_type'] == 'judgment' and not raw.get('reasoning_prompt'):
        raise RuleValidationError(
            f'{source_file}: rule {raw["rule_id"]!r} is judgment-based but has no '
            f'reasoning_prompt'
        )
    status = raw.get('validation_status', 'pending_pilot_validation')
    if status not in VALIDATION_STATUSES:
        raise RuleValidationError(
            f'{source_file}: rule {raw["rule_id"]!r} validation_status={status!r}, '
            f'must be one of {VALIDATION_STATUSES}'
        )


def _to_rule(raw: dict, source_file: str) -> Rule:
    return Rule(
        rule_id=raw['rule_id'],
        title=raw['title'],
        applicable_drawing_types=tuple(raw['applicable_drawing_types']),
        applicable_components=tuple(raw.get('applicable_components') or ()),
        required_entities=tuple(raw['required_entities']),
        rule_type=raw['rule_type'],
        formula=raw.get('formula'),
        reasoning_prompt=raw.get('reasoning_prompt'),
        unit=raw.get('unit'),
        severity=raw.get('severity', 'error'),
        category=raw['category'],
        message_template=raw.get('message_template'),
        engineering_rationale=raw['engineering_rationale'],
        source_reference=raw['source_reference'],
        validation_status=raw.get('validation_status', 'pending_pilot_validation'),
        source_file=source_file,
    )


def load_rules(rules_dir: str | None = None) -> list[Rule]:
    """
    Load and validate every rule from every *.yaml file in rules_dir
    (default: the `rules/` subdirectory next to this file).
    Raises RuleValidationError on any malformed rule or duplicate rule_id —
    a bad rule file must never reach production silently degraded.
    """
    if rules_dir is None:
        rules_dir = os.path.join(os.path.dirname(__file__), 'rules')

    rules: list[Rule] = []
    seen_ids: dict[str, str] = {}
    for path in sorted(glob.glob(os.path.join(rules_dir, '*.yaml'))):
        source_file = os.path.basename(path)
        with open(path, 'r', encoding='utf-8') as f:
            doc = yaml.safe_load(f) or []
        if not isinstance(doc, list):
            raise RuleValidationError(f'{source_file}: top-level YAML must be a list of rules')
        for raw in doc:
            _validate_raw(raw, source_file)
            rule_id = raw['rule_id']
            if rule_id in seen_ids:
                raise RuleValidationError(
                    f'{source_file}: duplicate rule_id {rule_id!r} '
                    f'(already defined in {seen_ids[rule_id]})'
                )
            seen_ids[rule_id] = source_file
            rules.append(_to_rule(raw, source_file))

    log.info('Loaded %d knowledge rules from %s (%d deterministic, %d judgment)',
              len(rules), rules_dir,
              sum(1 for r in rules if r.rule_type == 'deterministic'),
              sum(1 for r in rules if r.rule_type == 'judgment'))
    return rules
