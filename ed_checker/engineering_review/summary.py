"""
Builds the compact text digest fed to the engineering reviewer alongside the
drawing image and retrieved concepts. Curated, not a raw dict dump — the
reviewer should reason from established facts (schedule values, geometry,
notes, what's already been flagged), not re-derive or guess at them from a
huge blob of structured data.
"""
from __future__ import annotations


def _fmt_bar_row(mark: str, row: dict) -> str:
    parts = [f'dia={row.get("bar_dia_mm")}mm']
    if row.get('spacing_mm') is not None:
        parts.append(f'spacing={row["spacing_mm"]}mm')
    if row.get('count') is not None:
        parts.append(f'count={row["count"]}')
    if row.get('length_m') is not None:
        parts.append(f'length={row["length_m"]}m')
    return f'  {mark}: ' + ', '.join(parts)


def _fmt_schedule_component(component: str, marks: dict) -> list[str]:
    lines = [f'{component.upper()} schedule:']
    for mark, row in sorted(marks.items()):
        if isinstance(row, list):
            for i, zone in enumerate(row):
                lines.append(_fmt_bar_row(f'{mark} (zone {i + 1})', zone))
        elif isinstance(row, dict):
            lines.append(_fmt_bar_row(mark, row))
    return lines


def build_structured_summary(drawing_data: dict, issues: list[dict]) -> str:
    lines = ['DRAWING SUMMARY', '']

    schedule = (drawing_data or {}).get('schedule', {}) or {}
    components = sorted(schedule.keys())
    lines.append(f'Components present: {", ".join(components) or "(none detected)"}')
    lines.append('')

    for component in components:
        marks = schedule.get(component) or {}
        if not marks:
            continue
        lines.extend(_fmt_schedule_component(component, marks))
        lines.append('')

    geometry = (drawing_data or {}).get('geometry_from_drawing', {}) or {}
    if geometry:
        lines.append('Geometry (dimensions extracted from drawing):')
        for key, val in sorted(geometry.items()):
            lines.append(f'  {key}: {val}')
        lines.append('')

    notes = (drawing_data or {}).get('notes', {}) or {}
    note_items = {k: v for k, v in notes.items() if k != 'bbox' and v is not None}
    if note_items:
        lines.append('Notes:')
        for key, val in sorted(note_items.items()):
            lines.append(f'  {key}: {val}')
        lines.append('')

    if issues:
        lines.append(
            'ALREADY-FLAGGED ISSUES (from the deterministic/rule/vision checks that already '
            'ran — for reference only, do not re-flag these individually; consider what they '
            'might indicate holistically):'
        )
        for issue in issues:
            lines.append(f'  [{issue.get("category", "General")}] {issue.get("title", "")}')
        lines.append('')
    else:
        lines.append('ALREADY-FLAGGED ISSUES: none found by the other checks.')
        lines.append('')

    return '\n'.join(lines)
