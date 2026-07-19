"""
Concept schema + YAML loader for the engineering-reasoning knowledge base.

A "concept" is a piece of conceptual/narrative engineering knowledge (why
cover exists, how load transfers through a pile cap, common consultant
mistakes) — deliberately distinct from a knowledge_rules.Rule. A Rule is
individually evaluated (a deterministic formula or a single yes/no judgment
call); a Concept is never evaluated on its own — it's retrieved as context
and handed to the engineering reviewer's synthesis prompt alongside several
others. See ed_checker/knowledge_rules/schema.py for the sibling rule schema
this deliberately does NOT reuse (different shape, different purpose).

Validation is strict and fails at load time, same rationale as the rule
loader: a malformed concept is a content bug, not a runtime condition.
"""
from __future__ import annotations

import glob
import logging
import os
from dataclasses import dataclass, field

import yaml

log = logging.getLogger(__name__)

_REQUIRED_FIELDS = (
    'concept_id', 'title', 'applicable_drawing_types', 'applicable_components',
    'body', 'source_reference',
)


class ConceptValidationError(ValueError):
    """A concept YAML entry is malformed — fails loudly at load time."""


@dataclass(frozen=True)
class Concept:
    concept_id: str
    title: str
    applicable_drawing_types: tuple[str, ...]
    applicable_components: tuple[str, ...]
    body: str
    source_reference: str
    topic_tags: tuple[str, ...] = field(default_factory=tuple)
    source_file: str = ''   # populated by the loader, not user-supplied


def _validate_raw(raw: dict, source_file: str) -> None:
    missing = [f for f in _REQUIRED_FIELDS if not raw.get(f)]
    if missing:
        raise ConceptValidationError(
            f'{source_file}: concept {raw.get("concept_id", "<no concept_id>")!r} missing '
            f'required field(s): {missing}'
        )


def _to_concept(raw: dict, source_file: str) -> Concept:
    return Concept(
        concept_id=raw['concept_id'],
        title=raw['title'],
        applicable_drawing_types=tuple(raw['applicable_drawing_types']),
        applicable_components=tuple(raw['applicable_components']),
        body=raw['body'],
        source_reference=raw['source_reference'],
        topic_tags=tuple(raw.get('topic_tags') or ()),
        source_file=source_file,
    )


def load_concepts(concepts_dir: str | None = None) -> list[Concept]:
    """
    Load and validate every concept from every *.yaml file in concepts_dir
    (default: the `concepts/` subdirectory next to this file).
    Raises ConceptValidationError on any malformed concept or duplicate
    concept_id — a bad concept file must never reach production silently
    degraded.
    """
    if concepts_dir is None:
        concepts_dir = os.path.join(os.path.dirname(__file__), 'concepts')

    concepts: list[Concept] = []
    seen_ids: dict[str, str] = {}
    for path in sorted(glob.glob(os.path.join(concepts_dir, '*.yaml'))):
        source_file = os.path.basename(path)
        with open(path, 'r', encoding='utf-8') as f:
            doc = yaml.safe_load(f) or []
        if not isinstance(doc, list):
            raise ConceptValidationError(f'{source_file}: top-level YAML must be a list of concepts')
        for raw in doc:
            _validate_raw(raw, source_file)
            concept_id = raw['concept_id']
            if concept_id in seen_ids:
                raise ConceptValidationError(
                    f'{source_file}: duplicate concept_id {concept_id!r} '
                    f'(already defined in {seen_ids[concept_id]})'
                )
            seen_ids[concept_id] = source_file
            concepts.append(_to_concept(raw, source_file))

    log.info('Loaded %d engineering-reasoning concepts from %s', len(concepts), concepts_dir)
    return concepts
