"""
Compare structured design input data vs extracted drawing data.
Returns a list of issue dicts matching the DB schema.
"""
import re

# Fallback bounding boxes used only when Claude didn't return a bbox for that item.
# Right side of PPP drawing layout: left ~62% = views, right ~38% = schedule + title block
BBOX_FALLBACK = {
    'pilecap_schedule': {'x': 63, 'y': 22, 'w': 34, 'h':  4},  # single-row height
    'pile_schedule':    {'x': 63, 'y': 22, 'w': 34, 'h':  4},
    'pier_schedule':    {'x': 63, 'y': 22, 'w': 34, 'h':  4},
    'title_block':      {'x': 63, 'y': 77, 'w': 34, 'h': 20},
    'notes':            {'x': 63, 'y': 67, 'w': 34, 'h':  9},
    'table_1':          {'x': 82, 'y':  1, 'w': 16, 'h':  4},
    'default':          {'x': 63, 'y': 22, 'w': 34, 'h':  4},
}


_SCHEDULE_ZONES = {'pilecap_schedule', 'pile_schedule', 'pier_schedule', 'default'}

def _bbox(zone, drawing_bbox=None):
    """Return (x, y, w, h). Uses Claude-extracted bbox when plausible, else fallback."""
    if drawing_bbox and all(k in drawing_bbox for k in ('x', 'y', 'w', 'h')):
        b = drawing_bbox
        x, y, w, h = float(b['x']), float(b['y']), float(b['w']), float(b['h'])
        # Basic validity
        if 0 <= x <= 100 and 0 <= y <= 100 and 0 < w <= 60 and 0 < h <= 20:
            if zone in _SCHEDULE_ZONES:
                # Schedule rows must be on the right side of the drawing and
                # above the title block (which starts ~73% down)
                if x >= 40 and y <= 73 and h <= 10:
                    return x, y, w, h
            else:
                # Title block, notes, table_1 — trust Claude's coords
                return x, y, w, h
    b = BBOX_FALLBACK.get(zone, BBOX_FALLBACK['default'])
    return b['x'], b['y'], b['w'], b['h']


def _issue(category, title, description, suggestion, severity, zone, drawing_bbox=None):
    x, y, w, h = _bbox(zone, drawing_bbox)
    return {
        'category':    category,
        'title':       title,
        'description': description,
        'suggestion':  suggestion,
        'severity':    severity,
        'page_num':    1,
        'x': x, 'y': y, 'width': w, 'height': h,
    }


def _norm_dia(v):
    """Extract integer diameter from strings like '25Φ', '25ϕ', '25 DIA', '25mm'."""
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r'(\d+)', str(v))
    return int(m.group(1)) if m else None


def _norm_count(v):
    """Extract final integer count from strings like '4×13 = 52', '42', '10×20 = 200'."""
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v)
    # look for '= N' at end
    m = re.search(r'=\s*(\d+)', s)
    if m:
        return int(m.group(1))
    # or just the last number
    m = re.search(r'(\d+)\s*$', s)
    return int(m.group(1)) if m else None


def _norm_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct_diff(a, b):
    """% difference between two floats."""
    if a and b and a != 0:
        return abs(a - b) / a * 100
    return None


def compare(design_data: dict, drawing_data: dict) -> list:
    issues = []

    if not design_data:
        issues.append(_issue(
            'Input', 'No design input provided',
            'No design input file was uploaded. Most checks could not be performed.',
            'Upload the E2E design input Excel alongside the drawing.',
            'warning', 'default'
        ))
        return issues

    if not drawing_data or not drawing_data.get('schedule') and not drawing_data.get('title_block'):
        issues.append(_issue(
            'Extraction', 'Drawing data extraction failed',
            'Could not extract data from the drawing PDF. Ensure ANTHROPIC_API_KEY is set and the file is a valid PDF.',
            'Check server logs and API key configuration.',
            'error', 'default'
        ))
        return issues

    issues += _check_title_block(drawing_data.get('title_block') or {}, design_data)
    issues += _check_notes(drawing_data.get('notes') or {}, design_data)
    issues += _check_schedule(drawing_data.get('schedule') or {}, design_data)
    issues += _check_table1(drawing_data.get('table_1') or [], design_data)

    return issues


# ── Title Block ───────────────────────────────────────────────────────────────

def _check_title_block(tb: dict, design: dict) -> list:
    issues = []
    zone = 'title_block'

    required = ['drawing_number', 'revision', 'title', 'spans', 'drawn_by', 'approved_by', 'date', 'scale']
    for field in required:
        if not tb.get(field):
            issues.append(_issue(
                'Title Block', f'Missing field: {field}',
                f'The field "{field}" is empty or could not be read from the title block.',
                f'Fill in the "{field}" field in the title block.',
                'error', zone
            ))

    # Revision format check
    rev = tb.get('revision', '')
    if rev and not re.match(r'^R\d+$', rev.strip()):
        issues.append(_issue(
            'Title Block', f'Revision format invalid: "{rev}"',
            f'Revision should be in format R0, R1, R2 etc. Found: "{rev}".',
            'Update revision to follow R<number> format.',
            'error', zone
        ))

    # Drawing number format
    drg = tb.get('drawing_number', '')
    if drg and not re.match(r'^[A-Z]+/[A-Z]+/[A-Z]+[-/]\d+[A-Z]?$', drg.replace(' ', '')):
        issues.append(_issue(
            'Title Block', f'Drawing number format check: "{drg}"',
            f'Drawing number "{drg}" — verify it follows the project numbering convention.',
            'Confirm drawing number matches the project register.',
            'warning', zone
        ))

    # Scale check
    scale = tb.get('scale', '')
    if scale and scale.upper() not in ('AS SHOWN', 'NTS') and not re.match(r'1\s*:\s*\d+', scale):
        issues.append(_issue(
            'Title Block', f'Scale value unusual: "{scale}"',
            f'Scale field shows "{scale}". Expected "AS SHOWN" or a ratio like "1:50".',
            'Verify scale field.',
            'warning', zone
        ))

    return issues


# ── Notes ─────────────────────────────────────────────────────────────────────

def _check_notes(notes: dict, design: dict) -> list:
    issues = []
    zone = 'notes'
    geo = design.get('geometry', {})

    # Lap length concrete grade vs pile concrete grade
    lap_grade = notes.get('lap_length_concrete_grade')
    pile_grade = notes.get('concrete_pile')
    if lap_grade and pile_grade:
        if lap_grade.upper() != pile_grade.upper():
            issues.append(_issue(
                'Notes', f'Lap length table references {lap_grade} but pile concrete is {pile_grade}',
                f'The lap length table in the drawing references {lap_grade} concrete, '
                f'but the pile concrete grade is {pile_grade}. These must match.',
                f'Update lap length table to reference {pile_grade} concrete.',
                'error', zone
            ))
    elif not lap_grade:
        issues.append(_issue(
            'Notes', 'Lap length concrete grade not found',
            'Could not read which concrete grade the lap length table references.',
            'Ensure the lap length table header is legible and states the concrete grade.',
            'warning', zone
        ))

    # Pile geometry notes vs design input
    if geo:
        pile_len_note = _norm_float(notes.get('pile_length_m'))
        pile_len_design = geo.get('pile_length')
        if pile_len_note and pile_len_design:
            diff = _pct_diff(pile_len_design, pile_len_note)
            if diff and diff > 1:
                issues.append(_issue(
                    'Notes', f'Pile length mismatch: note says {pile_len_note}m, design says {pile_len_design}m',
                    f'Drawing note states pile length = {pile_len_note}m. Design input specifies {pile_len_design}m.',
                    f'Update pile length note to {pile_len_design}m.',
                    'error', zone
                ))

        pile_fix_note = _norm_float(notes.get('pile_fixity_m'))
        pile_fix_design = geo.get('pile_fixity')
        if pile_fix_note and pile_fix_design:
            diff = _pct_diff(pile_fix_design, pile_fix_note)
            if diff and diff > 1:
                issues.append(_issue(
                    'Notes', f'Pile fixity mismatch: note says {pile_fix_note}m, design says {pile_fix_design}m',
                    f'Drawing note states pile fixity = {pile_fix_note}m. Design input specifies {pile_fix_design}m.',
                    f'Update pile fixity note to {pile_fix_design}m.',
                    'error', zone
                ))

    return issues


# ── Schedule ──────────────────────────────────────────────────────────────────

COMPONENT_ZONE = {
    'pilecap': 'pilecap_schedule',
    'pile':    'pile_schedule',
    'pier':    'pier_schedule',
}


def _check_schedule(schedule: dict, design: dict) -> list:
    issues = []

    component_map = {
        'pilecap': design.get('pilecap_bbs', {}),
        'pile':    design.get('pile_bbs', {}),
        'pier':    design.get('pier_bbs', {}),
    }

    for comp, design_bbs in component_map.items():
        zone = COMPONENT_ZONE.get(comp, 'default')
        drawing_comp = schedule.get(comp, {})

        if not drawing_comp:
            if design_bbs:
                issues.append(_issue(
                    'Reinforcement', f'{comp.title()} schedule not found in drawing',
                    f'Could not extract the {comp} reinforcement schedule from the drawing.',
                    f'Ensure the {comp} schedule table is legible and present in the drawing.',
                    'warning', zone
                ))
            continue

        for bm, design_bar in design_bbs.items():
            # Handle list (duplicate marks like two 'y' rows)
            if isinstance(design_bar, list):
                design_bars = design_bar
            else:
                design_bars = [design_bar]

            drawing_bar = drawing_comp.get(bm)
            if not drawing_bar:
                issues.append(_issue(
                    'Reinforcement', f"Bar mark '{bm}' ({comp}) not found in drawing schedule",
                    f"Bar mark '{bm}' is specified in the design input but was not found in the drawing's {comp} schedule.",
                    f"Add bar mark '{bm}' to the {comp} schedule.",
                    'error', zone
                ))
                continue

            d_bar = design_bars[0]
            # Pass Claude's extracted bbox for this bar so highlights land on the right row
            bar_bbox = drawing_bar.get('bbox')
            issues += _compare_bar(bm, comp, d_bar, drawing_bar, zone, design_bars, bar_bbox)

    return issues


def _compare_bar(bm, comp, design_bar, drawing_bar, zone, all_design_bars=None, bar_bbox=None):
    issues = []
    prefix = f"Bar '{bm}' ({comp})"

    # Diameter check
    d_dia = design_bar.get('dia_mm')
    w_dia = _norm_dia(drawing_bar.get('bar_dia_mm') or drawing_bar.get('reinforcement_text', ''))
    if d_dia and w_dia and d_dia != w_dia:
        issues.append(_issue(
            'Reinforcement', f"{prefix}: Diameter mismatch — design {d_dia}mm, drawing {w_dia}mm",
            f"Design input specifies {d_dia}mm diameter for bar '{bm}' ({comp}). Drawing shows {w_dia}mm.",
            f"Change bar diameter to {d_dia}mm as per design input.",
            'error', zone, bar_bbox
        ))

    # Spacing check
    d_spacing = design_bar.get('spacing_mm')
    w_spacing = _norm_float(drawing_bar.get('spacing_mm'))
    if d_spacing and w_spacing:
        diff = _pct_diff(d_spacing, w_spacing)
        if diff and diff > 5:
            issues.append(_issue(
                'Reinforcement', f"{prefix}: Spacing mismatch — design {d_spacing}mm c/c, drawing {w_spacing}mm c/c",
                f"Design input specifies {d_spacing}mm c/c for bar '{bm}' ({comp}). Drawing shows {w_spacing}mm c/c.",
                f"Update spacing to {d_spacing}mm c/c.",
                'error', zone, bar_bbox
            ))
    elif d_spacing and not w_spacing:
        issues.append(_issue(
            'Reinforcement', f"{prefix}: Spacing not found in drawing ({d_spacing}mm expected)",
            f"Design input specifies spacing of {d_spacing}mm c/c for bar '{bm}' but drawing schedule does not show spacing.",
            f"Add {d_spacing}mm c/c spacing for bar '{bm}'.",
            'warning', zone, bar_bbox
        ))

    # Count check
    d_count = design_bar.get('count')
    if all_design_bars and len(all_design_bars) > 1:
        d_count = sum(b.get('count') or 0 for b in all_design_bars if b.get('count'))
    w_count = _norm_count(drawing_bar.get('count') or drawing_bar.get('count_text', ''))
    if d_count and w_count and d_count != w_count:
        issues.append(_issue(
            'Reinforcement', f"{prefix}: Count mismatch — design {d_count} nos, drawing {w_count} nos",
            f"Design input specifies {d_count} bars for '{bm}' ({comp}). Drawing schedule shows {w_count} bars.",
            f"Update count to {d_count} nos.",
            'error', zone, bar_bbox
        ))

    # Length check
    d_len = design_bar.get('length_m')
    w_len = _norm_float(drawing_bar.get('length_m'))
    if d_len and w_len:
        diff = _pct_diff(d_len, w_len)
        if diff and diff > 2:
            issues.append(_issue(
                'Reinforcement', f"{prefix}: Bar length mismatch — design {d_len}m, drawing {w_len}m",
                f"Design input bar length = {d_len}m for '{bm}' ({comp}). Drawing shows {w_len}m.",
                f"Update bar length to {d_len}m.",
                'error', zone, bar_bbox
            ))

    # Weight sanity check
    w_total_wt = _norm_float(drawing_bar.get('total_wt_kg'))
    w_total_len = _norm_float(drawing_bar.get('total_length_m'))
    w_unit_wt = _norm_float(drawing_bar.get('unit_wt_kg_m'))
    if w_total_len and w_unit_wt and w_total_wt:
        calc_wt = w_total_len * w_unit_wt
        diff = _pct_diff(calc_wt, w_total_wt)
        if diff and diff > 2:
            issues.append(_issue(
                'Reinforcement', f"{prefix}: Schedule weight arithmetic error",
                f"Bar '{bm}' ({comp}): total length × unit weight = {calc_wt:.1f}kg but schedule shows {w_total_wt:.1f}kg.",
                f"Recheck weight calculation for bar '{bm}'.",
                'error', zone, bar_bbox
            ))

    return issues


# ── TABLE-1 ───────────────────────────────────────────────────────────────────

def _check_table1(table1: list, design: dict) -> list:
    issues = []
    if not table1:
        issues.append(_issue(
            'Levels (TABLE-1)', 'TABLE-1 not found or not legible',
            'Could not extract TABLE-1 (pier level/elevation data) from the drawing.',
            'Ensure TABLE-1 is present and legible in the drawing.',
            'warning', 'table_1'
        ))
        return issues

    geo = design.get('geometry', {})
    pilecap_depth = geo.get('pilecap_depth')

    for row in table1:
        pier = row.get('pier_id', '?')
        top_pc = _norm_float(row.get('top_pilecap_m'))
        bot_pc = _norm_float(row.get('bottom_pilecap_m'))

        if top_pc and bot_pc and pilecap_depth:
            drawn_depth = round(top_pc - bot_pc, 3)
            expected = round(pilecap_depth, 3)
            if abs(drawn_depth - expected) > 0.01:
                issues.append(_issue(
                    'Levels (TABLE-1)', f'Pier {pier}: Pilecap depth mismatch — TABLE-1 shows {drawn_depth}m, design specifies {expected}m',
                    f'For pier {pier}: top of pilecap ({top_pc}) − bottom of pilecap ({bot_pc}) = {drawn_depth}m. '
                    f'Design input specifies pilecap depth = {expected}m.',
                    f'Correct the pilecap level values in TABLE-1 for pier {pier}.',
                    'error', 'table_1'
                ))

    return issues
