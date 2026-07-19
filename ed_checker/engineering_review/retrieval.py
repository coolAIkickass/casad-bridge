"""
Structured retrieval for engineering-reasoning concepts — deliberately NOT
embeddings-based (same rationale as ed_checker/knowledge_rules/retrieval.py):
auditable ("given this drawing, print exactly which concepts were retrieved
and why") beats fuzzy semantic recall at the pilot content volume (~10-20
concepts) this starts with. Revisit only if/when the concept library grows
large enough that tag-based retrieval demonstrably starts missing relevant
connections in practice.

Two-filter match, same shape as the rule engine's retrieval: drawing_type
must be in applicable_drawing_types, AND at least one applicable_component
must be a component actually detected in this drawing. No entity-presence
check (concepts aren't tied to a specific field existing) and no soft cap
yet (pilot volume is small enough that every drawing-type/component match is
fine to include in full).
"""
from __future__ import annotations

import logging

from .schema import Concept

log = logging.getLogger(__name__)


def _detected_components(drawing_data: dict) -> set[str]:
    """Components with at least a schedule entry — same signal
    detect_drawing_type() uses ('pile' in schedule or 'pilecap' in schedule)."""
    return set((drawing_data or {}).get('schedule', {}).keys())


def get_relevant_concepts(concepts: list[Concept], drawing_type: str,
                           drawing_data: dict) -> list[Concept]:
    """
    Filter the full concept library down to what applies to this specific
    drawing: drawing_type match + at least one applicable_component actually
    present. drawing_type here is the short profile name (e.g. 'ppp'), same
    convention as knowledge_rules.get_applicable_rules.
    """
    components = _detected_components(drawing_data)
    matched = []
    for concept in concepts:
        if drawing_type not in concept.applicable_drawing_types:
            continue
        if components and not (set(concept.applicable_components) & components):
            continue
        matched.append(concept)

    log.info('Retrieved %d/%d engineering-reasoning concept(s) for drawing_type=%r components=%s: %s',
              len(matched), len(concepts), drawing_type, sorted(components),
              [c.concept_id for c in matched])
    return matched
