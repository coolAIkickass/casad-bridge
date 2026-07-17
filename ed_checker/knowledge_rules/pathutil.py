"""Shared dotted-path resolution against a drawing_data dict (schema.py's shape).
Used by both retrieval.py (presence checks) and evaluator.py (value lookups).
"""
from __future__ import annotations


def resolve_path(drawing_data: dict, path: str):
    """
    Resolve a dotted path (e.g. 'notes.clear_cover_mm', 'schedule.pilecap')
    against drawing_data. Returns the resolved value, or None if any segment
    is missing. No wildcard syntax — a path always names one exact key at
    each level, matching drawing_data's own fixed schema (schema.py).
    """
    node = drawing_data
    for segment in path.split('.'):
        if not isinstance(node, dict) or segment not in node:
            return None
        node = node[segment]
    return node
