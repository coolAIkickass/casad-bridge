"""
Compare structured design input data vs extracted drawing data.
Returns a list of issue dicts matching the DB schema.
"""
import re

# Fallback section bboxes for each schedule component and other zones.
# Used when Claude doesn't return section bboxes.
# Right side of PPP drawing layout: left ~62% = views, right ~38% = schedule + title block.
# Schedule order top-to-bottom: pilecap → pile → pier (each occupies a section of the right strip).
BBOX_FALLBACK = {
    'pilecap_schedule': {'x': 62, 'y': 10, 'w': 35, 'h': 22},
    'pile_schedule':    {'x': 62, 'y': 32, 'w': 35, 'h': 15},
    'pier_schedule':    {'x': 62, 'y': 47, 'w': 35, 'h': 22},
    'title_block':      {'x': 63, 'y': 77, 'w': 34, 'h': 20},
    'notes':            {'x': 63, 'y': 67, 'w': 34, 'h':  9},
    'table_1':          {'x': 82, 'y':  1, 'w': 16, 'h':  4},
    'default':          {'x': 63, 'y': 22, 'w': 34, 'h':  4},
}


def _valid_section_bbox(b):
    """Return True if b is a plausible schedule section-level bbox.
    x must be >= 55 — the schedule is always in the right portion of a PPP drawing."""
    if not b or not all(k in b for k in ('x', 'y', 'w', 'h')):
        return False
    x, y, w, h = float(b['x']), float(b['y']), float(b['w']), float(b['h'])
    return 55 <= x <= 85 and 0 <= y <= 85 and 10 <= w <= 45 and 4 <= h <= 70


def _get_section_bbox(zone, section_bboxes):
    """Return section bbox dict for zone, from Claude or fallback."""
    comp_key = zone.replace('_schedule', '')   # 'pilecap_schedule' → 'pilecap'
    claude_bbox = (section_bboxes or {}).get(comp_key)
    if _valid_section_bbox(claude_bbox):
        return claude_bbox
    fb = BBOX_FALLBACK.get(zone, BBOX_FALLBACK['default'])
    return {'x': fb['x'], 'y': fb['y'], 'w': fb['w'], 'h': fb['h']}


def _bbox(zone, drawing_bbox=None):
    """Return (x, y, w, h) for non-schedule items (title block, notes, views)."""
    if drawing_bbox and all(k in drawing_bbox for k in ('x', 'y', 'w', 'h')):
        b = drawing_bbox
        x, y, w, h = float(b['x']), float(b['y']), float(b['w']), float(b['h'])
        # Generous limits — section views can be tall (h up to 50%) and wide (w up to 80%)
        if 0 <= x <= 100 and 0 <= y <= 100 and 0 < w <= 80 and 0 < h <= 50:
            return x, y, w, h
    fb = BBOX_FALLBACK.get(zone, BBOX_FALLBACK['default'])
    return fb['x'], fb['y'], fb['w'], fb['h']


def _find_section_bbox(text: str, sections: list,
                        section_view_positions: dict = None) -> dict | None:
    """
    Look for a view/section name in the issue text.
    Priority: 1) pdfplumber section_view_positions (exact coords from PDF text)
              2) Claude's sections list (estimated)
    """
    if not text:
        return None
    text_upper = text.upper()

    # 1. pdfplumber exact positions — keys are upper-cased line text (up to 60 chars)
    for name, bbox in (section_view_positions or {}).items():
        parts = [p for p in name.split() if len(p) >= 5]
        if name in text_upper or any(p in text_upper for p in parts):
            if bbox and all(k in bbox for k in ('x', 'y', 'w', 'h')):
                return bbox

    # 2. Claude's sections list
    for s in (sections or []):
        name = (s.get('name') or '').upper().strip()
        if not name:
            continue
        if name in text_upper:
            bbox = s.get('bbox')
            if bbox and all(k in bbox for k in ('x', 'y', 'w', 'h')):
                return bbox
        short = name.split('(')[0].strip()
        if len(short) >= 6 and short in text_upper:
            bbox = s.get('bbox')
            if bbox and all(k in bbox for k in ('x', 'y', 'w', 'h')):
                return bbox
    return None


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

    schedule_section_positions = drawing_data.get('schedule_section_positions') or {}
    section_view_positions     = drawing_data.get('section_view_positions') or {}
    section_bboxes             = drawing_data.get('schedule_section_bboxes') or {}
    sections                   = drawing_data.get('sections') or []

    issues += _check_title_block(drawing_data.get('title_block') or {}, design_data)
    issues += _check_notes(drawing_data.get('notes') or {}, design_data)
    issues += _check_schedule(
        drawing_data.get('schedule') or {}, design_data,
        section_bboxes, schedule_section_positions,
    )
    issues += _check_table1(drawing_data.get('table_1') or [], design_data)
    issues += _check_sections(sections)
    issues += _check_notes_completeness(
        drawing_data.get('notes_check') or [], sections, section_view_positions)
    issues += _check_label_issues(
        drawing_data.get('label_issues') or [], sections, section_view_positions)
    issues += _check_dimension_issues(
        drawing_data.get('dimension_issues') or [], sections, section_view_positions)

    return issues


# ── Title Block ───────────────────────────────────────────────────────────────

def _check_title_block(tb: dict, design: dict) -> list:
    issues = []
    zone = 'title_block'
    tb_bbox = tb.get('bbox')  # Claude's estimated bbox for the title block area

    required = ['drawing_number', 'revision', 'title', 'spans', 'drawn_by', 'approved_by', 'date', 'scale']
    for field in required:
        if not tb.get(field):
            issues.append(_issue(
                'Title Block', f'Missing field: {field}',
                f'The field "{field}" is empty or could not be read from the title block.',
                f'Fill in the "{field}" field in the title block.',
                'error', zone, tb_bbox
            ))

    # Revision format check
    rev = tb.get('revision', '')
    if rev and not re.match(r'^R\d+$', rev.strip()):
        issues.append(_issue(
            'Title Block', f'Revision format invalid: "{rev}"',
            f'Revision should be in format R0, R1, R2 etc. Found: "{rev}".',
            'Update revision to follow R<number> format.',
            'error', zone, tb_bbox
        ))

    # Drawing number format
    drg = tb.get('drawing_number', '')
    if drg and not re.match(r'^[A-Z]+/[A-Z]+/[A-Z]+[-/]\d+[A-Z]?$', drg.replace(' ', '')):
        issues.append(_issue(
            'Title Block', f'Drawing number format check: "{drg}"',
            f'Drawing number "{drg}" — verify it follows the project numbering convention.',
            'Confirm drawing number matches the project register.',
            'warning', zone, tb_bbox
        ))

    # Scale check
    scale = tb.get('scale', '')
    if scale and scale.upper() not in ('AS SHOWN', 'NTS') and not re.match(r'1\s*:\s*\d+', scale):
        issues.append(_issue(
            'Title Block', f'Scale value unusual: "{scale}"',
            f'Scale field shows "{scale}". Expected "AS SHOWN" or a ratio like "1:50".',
            'Verify scale field.',
            'warning', zone, tb_bbox
        ))

    return issues


# ── Notes ─────────────────────────────────────────────────────────────────────

def _check_notes(notes: dict, design: dict) -> list:
    issues = []
    zone = 'notes'
    geo = design.get('geometry', {})
    notes_bbox = notes.get('bbox')  # Claude's estimated bbox for the notes area

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
                'error', zone, notes_bbox
            ))
    elif not lap_grade:
        issues.append(_issue(
            'Notes', 'Lap length concrete grade not found',
            'Could not read which concrete grade the lap length table references.',
            'Ensure the lap length table header is legible and states the concrete grade.',
            'warning', zone, notes_bbox
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
                    'error', zone, notes_bbox
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
                    'error', zone, notes_bbox
                ))

    return issues


# ── Schedule ──────────────────────────────────────────────────────────────────

COMPONENT_ZONE = {
    'pilecap': 'pilecap_schedule',
    'pile':    'pile_schedule',
    'pier':    'pier_schedule',
}

# Ring/confinement bars where a ±2-per-pile count variation is acceptable
# (bar count is derived from geometric spacing and can vary by 1–2 due to rounding).
RING_BAR_MARKS = {
    'pile':    {'y', 'y1', 'z'},
    'pier':    {'i', 'i1'},
    'pilecap': {'e'},
}

# Expected top-to-bottom order of bar marks in each component's schedule.
# Used to distribute row highlight boxes correctly when Claude provides no per-row bboxes.
CANONICAL_BAR_ORDER = {
    'pilecap': ['a', 'b', 'c', 'd', 'e', 'f', 'f1'],
    'pile':    ['x', 'y', 'y1', 'z'],
    'pier':    ['g', 'i', 'i1', 'j', 'j1', 'k', 'k1'],
}


def _check_schedule(schedule: dict, design: dict, section_bboxes: dict = None,
                    schedule_section_positions: dict = None) -> list:
    issues = []
    num_piles = int((design.get('geometry') or {}).get('pile_count') or 1)

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

        # Section bbox: prefer pdfplumber (reliable header detection) then Claude, then fallback
        plumber_sect = (schedule_section_positions or {}).get(comp)
        claude_sect  = _get_section_bbox(zone, section_bboxes)
        sect = plumber_sect if plumber_sect else claude_sect

        # Stable canonical ordering so row-index distribution matches the actual schedule
        _order = {bm: i for i, bm in enumerate(CANONICAL_BAR_ORDER.get(comp, []))}
        sorted_bms = sorted(
            drawing_comp.keys(),
            key=lambda bm: (_order.get(bm, 999), bm)
        )
        total = max(len(sorted_bms), 1)

        for bm, design_bar in design_bbs.items():
            if isinstance(design_bar, list):
                design_bars = design_bar
            else:
                design_bars = [design_bar]

            drawing_bar = drawing_comp.get(bm)
            if not drawing_bar:
                issues.append({
                    'category':    'Reinforcement',
                    'title':       f"Bar mark '{bm}' ({comp}) not found in drawing schedule",
                    'description': f"Bar mark '{bm}' is specified in the design input but was not found in the drawing's {comp} schedule.",
                    'suggestion':  f"Add bar mark '{bm}' to the {comp} schedule.",
                    'severity':    'error',
                    'page_num':    1,
                    'x': None, 'y': None, 'width': None, 'height': None,
                })
                continue

            d_bar = design_bars[0]

            # Distribute bars evenly within the section bbox using canonical row ordering
            try:
                row_idx = sorted_bms.index(bm)
            except ValueError:
                row_idx = total - 1
            row_h = max(sect['h'] / total, 2.0)
            bar_bbox = {
                'x': sect['x'],
                'y': sect['y'] + row_idx * row_h,
                'w': sect['w'],
                'h': row_h,
            }

            is_ring = bm in RING_BAR_MARKS.get(comp, set())
            issues += _compare_bar(bm, comp, d_bar, drawing_bar, zone, design_bars, bar_bbox,
                                   is_ring=is_ring, num_piles=num_piles)

    return issues


def _compare_bar(bm, comp, design_bar, drawing_bar, zone, all_design_bars=None, bar_bbox=None,
                 is_ring=False, num_piles=1):
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
                'Reinforcement', f"{prefix}: Spacing mismatch — design {round(d_spacing, 2)}mm c/c, drawing {round(w_spacing, 2)}mm c/c",
                f"Design input specifies {round(d_spacing, 2)}mm c/c for bar '{bm}' ({comp}). Drawing shows {round(w_spacing, 2)}mm c/c.",
                f"Update spacing to {round(d_spacing, 2)}mm c/c.",
                'error', zone, bar_bbox
            ))
    elif d_spacing and not w_spacing:
        issues.append(_issue(
            'Reinforcement', f"{prefix}: Spacing not found in drawing ({round(d_spacing, 2)}mm expected)",
            f"Design input specifies spacing of {round(d_spacing, 2)}mm c/c for bar '{bm}' but drawing schedule does not show spacing.",
            f"Add {round(d_spacing, 2)}mm c/c spacing for bar '{bm}'.",
            'warning', zone, bar_bbox
        ))

    # Count check
    d_count = design_bar.get('count')
    if all_design_bars and len(all_design_bars) > 1:
        d_count = sum(b.get('count') or 0 for b in all_design_bars if b.get('count'))
    w_count = _norm_count(drawing_bar.get('count') or drawing_bar.get('count_text', ''))
    if d_count and w_count:
        if is_ring:
            # Ring bars: allow ±2 per pile absolute tolerance (count derives from geometric
            # spacing and varies by 1–2 due to length rounding). Flag as warning only.
            tolerance = 2 * max(num_piles, 1)
            if abs(d_count - w_count) > tolerance:
                issues.append(_issue(
                    'Reinforcement', f"{prefix}: Count mismatch — design {d_count} nos, drawing {w_count} nos",
                    f"Design input specifies {d_count} bars for '{bm}' ({comp}). Drawing schedule shows {w_count} bars. "
                    f"Ring bar counts can vary by ±{tolerance} due to spacing rounding — verify manually.",
                    f"Check ring count for bar '{bm}' against Detail A/A' selection.",
                    'warning', zone, bar_bbox
                ))
        elif d_count != w_count:
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
                'Reinforcement', f"{prefix}: Bar length mismatch — design {round(d_len, 2)}m, drawing {round(w_len, 2)}m",
                f"Design input bar length = {round(d_len, 2)}m for '{bm}' ({comp}). Drawing shows {round(w_len, 2)}m.",
                f"Update bar length to {round(d_len, 2)}m.",
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
        row_bbox = row.get('bbox')  # Claude's estimated bbox for this TABLE-1 row

        if top_pc and bot_pc and pilecap_depth:
            drawn_depth = round(top_pc - bot_pc, 3)
            expected = round(pilecap_depth, 3)
            if abs(drawn_depth - expected) > 0.01:
                issues.append(_issue(
                    'Levels (TABLE-1)', f'Pier {pier}: Pilecap depth mismatch — TABLE-1 shows {drawn_depth}m, design specifies {expected}m',
                    f'For pier {pier}: top of pilecap ({top_pc}) − bottom of pilecap ({bot_pc}) = {drawn_depth}m. '
                    f'Design input specifies pilecap depth = {expected}m.',
                    f'Correct the pilecap level values in TABLE-1 for pier {pier}.',
                    'error', 'table_1', row_bbox
                ))

    return issues


# ── Sections / views ──────────────────────────────────────────────────────────

REQUIRED_SECTIONS = [
    'SECTION A-A FOR PILE',
    'SECTION Z-Z (PILE)',
    'SECTION A-A FOR PILECAP & PIER',
    'SECTION B-B FOR PILECAP & PIER',
    'PLAN OF PILECAP',
    'REINFORCEMENT PLAN OF PILECAP',
    'DETAIL A',
    'TABLE-1',
    'LAP LENGTH TABLE',
    'SCHEDULE OF REINFORCEMENT',
]

def _check_sections(sections: list) -> list:
    issues = []
    if not sections:
        issues.append(_issue(
            'Drawing Completeness', 'Could not verify required sections',
            'The drawing review did not return a sections inventory. Required views could not be checked.',
            'Ensure ANTHROPIC_API_KEY is configured and the drawing is legible.',
            'warning', 'default'
        ))
        return issues

    found_names = {s.get('name', '').upper() for s in sections if s.get('present')}

    for req in REQUIRED_SECTIONS:
        present = any(req.upper() in name for name in found_names)
        if not present:
            issues.append(_issue(
                'Drawing Completeness', f'Missing view: {req}',
                f'"{req}" was not found in the drawing.',
                f'Add the "{req}" view to the drawing.',
                'error', 'default'
            ))

    # Check each present section has a scale
    for sec in sections:
        if sec.get('present') and not sec.get('scale'):
            issues.append(_issue(
                'Drawing Completeness', f'Scale missing on {sec.get("name", "view")}',
                f'The view "{sec.get("name")}" does not show a scale (e.g. SCALE 1:30).',
                'Add a scale label to this view.',
                'warning', 'default', sec.get('bbox')
            ))

    return issues


# ── Notes completeness ────────────────────────────────────────────────────────

REQUIRED_NOTES = {
    'pile_length':       'Pile length not specified in notes',
    'pile_fixity':       'Pile fixity length not specified in notes',
    'pile_diameter':     'Pile diameter not specified in notes',
    'concrete_pile':     'Concrete grade for pile not specified in notes',
    'concrete_pilecap':  'Concrete grade for pilecap not specified in notes',
    'concrete_pier':     'Concrete grade for pier not specified in notes',
    'steel_grade':       'Steel grade (Fe415/Fe500/Fe550) not specified in notes',
}

def _check_notes_completeness(notes_check: list, sections: list = None,
                               section_view_positions: dict = None) -> list:
    issues = []
    if not notes_check:
        return issues

    found = {n.get('item'): n for n in notes_check}

    # If ANY concrete grade is present, the others are implied by the single-grade convention.
    concrete_keys = ('concrete_pile', 'concrete_pilecap', 'concrete_pier')
    any_concrete_found = any(
        found.get(k) and found[k].get('present') for k in concrete_keys
    )

    for item_key, missing_msg in REQUIRED_NOTES.items():
        entry = found.get(item_key)
        if not entry or not entry.get('present'):
            bbox = (entry.get('bbox') if entry else None) or _find_section_bbox(
                missing_msg, sections, section_view_positions)
            # Concrete grades are always warnings — a single grade note covers all components.
            # Steel grade is an error (must be explicit). Other missing notes are warnings.
            if item_key in concrete_keys:
                if any_concrete_found:
                    # Another component's grade was found — single-grade convention covers this.
                    continue
                severity = 'warning'
            elif item_key == 'steel_grade':
                severity = 'error'
            else:
                severity = 'warning'
            issues.append(_issue(
                'Notes', missing_msg,
                f'The note for "{item_key.replace("_", " ")}" is missing or not legible in the drawing.',
                f'Add the required note for {item_key.replace("_", " ")}.',
                severity, 'notes', bbox
            ))

    return issues


# ── Label & annotation quality ────────────────────────────────────────────────

def _check_label_issues(label_issues: list, sections: list = None,
                         section_view_positions: dict = None) -> list:
    issues = []
    for li in (label_issues or []):
        desc = li.get('description', '')
        if not desc:
            continue
        bbox = li.get('bbox') or _find_section_bbox(
            desc + ' ' + li.get('suggestion', ''), sections, section_view_positions
        )
        issues.append(_issue(
            'Labels & Annotations', desc, desc,
            li.get('suggestion', 'Review and correct this label.'),
            'warning', 'default', bbox
        ))
    return issues


# ── Dimension completeness ────────────────────────────────────────────────────

def _check_dimension_issues(dimension_issues: list, sections: list = None,
                              section_view_positions: dict = None) -> list:
    issues = []
    for di in (dimension_issues or []):
        desc = di.get('description', '')
        if not desc:
            continue
        bbox = di.get('bbox') or _find_section_bbox(
            desc + ' ' + di.get('suggestion', ''), sections, section_view_positions
        )
        issues.append(_issue(
            'Dimensions', desc, desc,
            di.get('suggestion', 'Add the missing dimension.'),
            'warning', 'default', bbox
        ))
    return issues
