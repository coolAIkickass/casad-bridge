"""
Compare structured design input data vs extracted drawing data.
Returns a list of issue dicts matching the DB schema.
"""
import re
import logging
from .profiles import PPP_PROFILE

log = logging.getLogger(__name__)

_REINF_RE_DIA     = re.compile(r'(\d+)\s*[φΦ]', re.IGNORECASE)
_REINF_RE_SPACING = re.compile(r'@\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*c/c', re.IGNORECASE)
_REINF_RE_COUNT   = re.compile(
    r'[*×]\s*(\d+)|(?:–|-)\s*(\d+)\s*NOS|(\d+)\s*NOS', re.IGNORECASE)


def _parse_reinf_text(text: str) -> dict:
    """Parse reinforcement column text like '16φ@150 c/c' or '25φ – 42 NOS.'.
    Returns dict with keys: dia (int|None), secondary_type ('spacing'|'count'|None),
    secondary_val (float|int|None)."""
    s = str(text or '').strip()
    dia_m = _REINF_RE_DIA.search(s)
    dia = int(dia_m.group(1)) if dia_m else None
    sp_m = _REINF_RE_SPACING.search(s)
    if sp_m:
        val = float(sp_m.group(1) or sp_m.group(2))
        return {'dia': dia, 'secondary_type': 'spacing', 'secondary_val': val}
    cnt_m = _REINF_RE_COUNT.search(s)
    if cnt_m:
        val = int(cnt_m.group(1) or cnt_m.group(2) or cnt_m.group(3))
        return {'dia': dia, 'secondary_type': 'count', 'secondary_val': val}
    return {'dia': dia, 'secondary_type': None, 'secondary_val': None}

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


def _parse_bundle_factor(reinforcement_text: str) -> int:
    """Return 2 if reinforcement_text indicates bundle/legged bars, else 1."""
    if not reinforcement_text:
        return 1
    t = str(reinforcement_text).upper()
    return 2 if ('BUNDLE' in t or 'LEGGED' in t) else 1


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


def _strip_1prefix_misread(w_val: float, d_vals_mm: list) -> float:
    """Correct the 'bar mark digit bleed' misread: if w_val has a leading '1' prepended
    to a value that matches one of the design dims (e.g. 1300 when design has 300),
    return the stripped value. Only triggers when the stripped value is within 3% of a
    known design dim. Leaves w_val unchanged otherwise."""
    w_str = f"{int(round(w_val))}"
    if len(w_str) >= 2 and w_str[0] == '1':
        stripped = float(w_str[1:])
        if any(abs(stripped - d) / d < 0.03 for d in d_vals_mm if d > 0):
            return stripped
    return w_val


def compare(design_data: dict, drawing_data: dict) -> list:
    issues = []

    if not design_data:
        issues.append(_issue(
            'Input', 'No design input provided',
            'No design input file was uploaded. Most checks could not be performed.',
            'Upload the E2E design input Excel alongside the drawing.',
            'error', 'default'
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
    # What the extraction path can vouch for (see schema.DEFAULT_CAPABILITIES).
    # Missing key → assume capable, preserving behaviour for stored/legacy data.
    capabilities               = drawing_data.get('capabilities') or {}

    issues += _check_extraction_diagnostics(drawing_data.get('extraction_diagnostics') or [])
    issues += _check_title_block(drawing_data.get('title_block') or {}, design_data)
    issues += _check_notes(drawing_data.get('notes') or {}, design_data)
    issues += _check_schedule(
        drawing_data.get('schedule') or {}, design_data,
        section_bboxes, schedule_section_positions,
        capabilities,
        dxf_comp_anchors=drawing_data.get('dxf_comp_anchors') or {},
    )
    # Section presence and notes completeness are now text-extracted (authoritative).
    # Supplement with inferences from extracted data: pdfplumber only scans the left 55%
    # of the page, so labels in the right/schedule area ('SCHEDULE OF REINFORCEMENT')
    # are invisible to it. If we successfully extracted the content, the label
    # must exist in the drawing — don't flag it as missing.
    # Note: TABLE-1 is deliberately not data-checked here (see _check_sections / required_sections
    # for the presence-only check) — CASAD always pastes TABLE-1 in as a picture/OLE object
    # copied directly from the design Excel, so its values can never diverge from design intent.
    _sft_raw = drawing_data.get('sections_from_text') or []
    _schedule = drawing_data.get('schedule') or {}
    _sft = []
    for _e in _sft_raw:
        _e2 = dict(_e)
        if not _e2.get('present'):
            _n = _e2.get('name', '')
            if _n == 'SCHEDULE OF REINFORCEMENT' and _schedule:
                _e2['present'] = True
        _sft.append(_e2)
    issues += _check_sections(_sft)
    issues += _check_notes_completeness(
        drawing_data.get('notes_completeness_from_text') or [], section_view_positions)
    issues += _check_label_issues(
        drawing_data.get('label_issues') or [], [], section_view_positions)
    issues += _check_dimension_issues(
        drawing_data.get('dimension_issues') or [], [], section_view_positions)
    issues += _check_cross_sections(drawing_data, design_data or {})
    issues += _check_cut_mark_references(drawing_data)
    issues += _check_unlabeled_views(drawing_data)
    issues += _compare_geometry_dims(drawing_data, design_data)
    issues += _check_bar_mark_callouts(drawing_data, design_data)

    seen_keys: set = set()
    deduped: list = []
    for iss in issues:
        key = (iss.get('category'), iss.get('title'))
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(iss)
    return deduped


# ── Extraction diagnostics ────────────────────────────────────────────────────

def _check_extraction_diagnostics(diagnostics: list) -> list:
    """
    Convert 'error' extraction diagnostics into review issues so the engineer can see
    what could NOT be checked. 'info' diagnostics stay in drawing_data for the debug
    route only. A silent extraction gap must never look like a clean check result.
    """
    issues = []
    for d in diagnostics:
        if d.get('severity') != 'error':
            continue
        code = d.get('code', '')
        msg  = d.get('message', 'Extraction degraded')
        if code == 'section_grade_mismatch':
            issues.append(_issue(
                'Notes', msg[:120], msg,
                'Correct the concrete grade annotation in the section view to match the notes.',
                'error', 'notes'
            ))
        else:
            issues.append(_issue(
                'Extraction', msg[:120], msg,
                'This item was not verified automatically — check it manually, or fix the '
                'input file and re-upload.',
                'error', 'default'
            ))
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
            'error', zone, tb_bbox
        ))

    return issues


# ── Notes ─────────────────────────────────────────────────────────────────────

def _check_notes(notes: dict, design: dict) -> list:
    issues = []
    zone = 'notes'
    geo = design.get('geometry', {})
    notes_bbox = notes.get('bbox')  # Claude's estimated bbox for the notes area

    # Lap length concrete grade vs pile concrete grade
    lap_grade  = notes.get('lap_length_concrete_grade')
    pile_grade = notes.get('concrete_pile')
    log.info('Notes check: lap_length_concrete_grade=%r concrete_pile=%r', lap_grade, pile_grade)
    if lap_grade and pile_grade:
        if lap_grade.upper() != pile_grade.upper():
            issues.append(_issue(
                'Notes', f'Lap length table references {lap_grade} but pile concrete is {pile_grade}',
                f'The lap length table in the drawing references {lap_grade} concrete, '
                f'but the pile concrete grade is {pile_grade}. These must match.',
                f'Update lap length table to reference {pile_grade} concrete.',
                'error', zone, notes_bbox
            ))
        else:
            log.info('Notes: lap grade %r matches pile grade %r — no issue', lap_grade, pile_grade)
    elif lap_grade and not pile_grade:
        log.info('Notes: lap_grade=%r found but concrete_pile not extracted — skipping comparison', lap_grade)
    elif not lap_grade:
        issues.append(_issue(
            'Notes', 'Lap length concrete grade not found',
            'Could not read which concrete grade the lap length table references.',
            'Ensure the lap length table header is legible and states the concrete grade.',
            'error', zone, notes_bbox
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

# Ring/confinement bar detection is now property-based: a bar with spacing_mm set in
# the design (c/c pitch column) is a confinement/ring bar and gets ±2 count tolerance.
# RING_BAR_MARKS is kept for reference only — it is NOT used in comparator logic.
# Do not add bar mark letters here as a workaround; fix the design Excel instead.
RING_BAR_MARKS = {}  # deprecated — retained so imports don't break


def _dxf_y_transform(dxf_anchors: dict, pdf_anchors: dict):
    """Compute (scale, offset) mapping DXF-extent-% y → PDF-page-% y.

    Uses PILECAP/PILE/PIER header rows as calibration anchors — the same
    physical text located in both DXF model-space and the PDF page.
    Returns None when fewer than 2 common anchors exist (can't fit a line).
    """
    pairs = [
        (dxf_anchors[c]['y'], pdf_anchors[c]['y'])
        for c in ('pilecap', 'pile', 'pier')
        if c in dxf_anchors and c in pdf_anchors
            and isinstance(pdf_anchors[c], dict) and 'y' in pdf_anchors[c]
    ]
    if len(pairs) < 2:
        return None
    # Use extreme pair for best spread / least noise
    y1_d, y1_p = pairs[0]
    y2_d, y2_p = pairs[-1]
    if abs(y2_d - y1_d) < 0.5:   # degenerate — anchors at same y
        return None
    scale  = (y2_p - y1_p) / (y2_d - y1_d)
    offset = y1_p - scale * y1_d
    return scale, offset


def _check_schedule(schedule: dict, design: dict, section_bboxes: dict = None,
                    schedule_section_positions: dict = None,
                    capabilities: dict = None,
                    dxf_comp_anchors: dict = None) -> list:
    issues = []
    num_piles = int((design.get('geometry') or {}).get('pile_count') or 1)
    # Spacing can only be flagged as missing if the extraction path reads a c/c column
    spacing_capable    = (capabilities or {}).get('spacing', True)
    shape_dims_capable = (capabilities or {}).get('shape_dims', True)

    # Pre-compute y-axis calibration transform (DXF path only).
    # When both DXF anchor positions and pdfplumber section positions are available,
    # use the component headers as anchor pairs to derive scale+offset so that
    # row_bbox y-values (DXF-extent-%) map accurately to PDF-page-%.
    y_transform = _dxf_y_transform(dxf_comp_anchors or {}, schedule_section_positions or {})

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
                    'error', zone
                ))
            continue

        # Section bbox: prefer pdfplumber (reliable header detection) then Claude, then fallback
        plumber_sect = (schedule_section_positions or {}).get(comp)
        claude_sect  = _get_section_bbox(zone, section_bboxes)
        sect = plumber_sect if plumber_sect else claude_sect

        # Use actual schedule insertion order (top-to-bottom in the drawing).
        sorted_bms = list(drawing_comp.keys())
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

            # Bar bbox strategy:
            # 1. DXF path + calibration available: transform row_bbox y using
            #    anchor-point calibration (most accurate — per-row DXF position).
            # 2. Fallback: distribute bars evenly within pdfplumber section bbox.
            dxf_row = drawing_bar.get('row_bbox')
            if dxf_row and y_transform:
                scale, offset = y_transform
                cal_y = max(0.0, min(99.0, scale * dxf_row['y'] + offset))
                cal_h = max(0.5, abs(scale) * dxf_row['h'])
                bar_bbox = {
                    'x': sect['x'],   # pdfplumber x/w — more reliable than DXF x
                    'y': cal_y,
                    'w': sect['w'],
                    'h': cal_h,
                }
            else:
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

            # A bar is a confinement/ring bar if the design specifies a c/c spacing.
            # Longitudinal and distributed bars don't have a c/c pitch column.
            is_ring = bool(d_bar.get('spacing_mm'))
            issues += _compare_bar(bm, comp, d_bar, drawing_bar, zone, design_bars, bar_bbox,
                                   is_ring=is_ring, num_piles=num_piles,
                                   spacing_capable=spacing_capable,
                                   shape_dims_capable=shape_dims_capable)

    return issues


def _compare_bar(bm, comp, design_bar, drawing_bar, zone, all_design_bars=None, bar_bbox=None,
                 is_ring=False, num_piles=1, spacing_capable=True, shape_dims_capable=True):
    issues = []
    prefix = f"Bar '{bm}' ({comp})"

    # Diameter check
    d_dia = design_bar.get('dia_mm')
    w_dia = _norm_dia(drawing_bar.get('bar_dia_mm') or drawing_bar.get('reinforcement_text', ''))
    if d_dia and w_dia and d_dia != w_dia:
        issues.append(_issue(
            'Bar Diameter', f"{prefix}: Diameter mismatch — design {d_dia}mm, drawing {w_dia}mm",
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
                'Bar Spacing', f"{prefix}: Spacing mismatch — design {round(d_spacing, 2)}mm c/c, drawing {round(w_spacing, 2)}mm c/c",
                f"Design input specifies {round(d_spacing, 2)}mm c/c for bar '{bm}' ({comp}). Drawing shows {round(w_spacing, 2)}mm c/c.",
                f"Update spacing to {round(d_spacing, 2)}mm c/c.",
                'error', zone, bar_bbox
            ))
    elif d_spacing and not w_spacing:
        if spacing_capable:
            # Only flag missing spacing when the extraction path can actually read a
            # c/c column (capability declared by the extractor — e.g. the DXF path sets
            # it False when the schedule has no spacing column).
            issues.append(_issue(
                'Bar Spacing', f"{prefix}: Spacing not found in drawing ({round(d_spacing, 2)}mm expected)",
                f"Design input specifies spacing of {round(d_spacing, 2)}mm c/c for bar '{bm}' but drawing schedule does not show spacing.",
                f"Add {round(d_spacing, 2)}mm c/c spacing for bar '{bm}'.",
                'error', zone, bar_bbox
            ))

    # Count check
    d_count = design_bar.get('count')
    if all_design_bars and len(all_design_bars) > 1:
        d_count = sum(b.get('count') or 0 for b in all_design_bars if b.get('count'))
    w_count = _norm_count(drawing_bar.get('count') or drawing_bar.get('count_text', ''))
    if d_count and w_count:
        if is_ring:
            # Ring bars: allow ±2 per pile absolute tolerance (count derives from geometric
            # spacing and varies by 1–2 due to length rounding).
            tolerance = 2 * max(num_piles, 1)
            if abs(d_count - w_count) > tolerance:
                issues.append(_issue(
                    'Bar Count', f"{prefix}: Count mismatch — design {d_count} nos, drawing {w_count} nos",
                    f"Design input specifies {d_count} bars for '{bm}' ({comp}). Drawing schedule shows {w_count} bars. "
                    f"Ring bar counts can vary by ±{tolerance} due to spacing rounding — verify manually.",
                    f"Check ring count for bar '{bm}' against Detail A/A' selection.",
                    'error', zone, bar_bbox
                ))
        elif d_count != w_count:
            issues.append(_issue(
                'Bar Count', f"{prefix}: Count mismatch — design {d_count} nos, drawing {w_count} nos",
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
                'Bar Length', f"{prefix}: Bar length mismatch — design {round(d_len, 2)}m, drawing {round(w_len, 2)}m",
                f"Design input bar length = {round(d_len, 2)}m for '{bm}' ({comp}). Drawing shows {round(w_len, 2)}m.",
                f"Update bar length to {round(d_len, 2)}m.",
                'error', zone, bar_bbox
            ))

    d_total_len = design_bar.get('total_len_m')
    if all_design_bars and len(all_design_bars) > 1:
        vals = [b.get('total_len_m') for b in all_design_bars if b.get('total_len_m')]
        if vals:
            d_total_len = sum(vals)
    w_total_len = _norm_float(drawing_bar.get('total_length_m'))
    if d_total_len and w_total_len:
        diff = _pct_diff(d_total_len, w_total_len)
        if diff and diff > 2:
            issues.append(_issue(
                'Bar Total Length',
                f"{prefix}: Total length mismatch — design {round(d_total_len, 2)}m, drawing {round(w_total_len, 2)}m",
                f"Design input total length = {round(d_total_len, 2)}m for '{bm}' ({comp}). Drawing schedule shows {round(w_total_len, 2)}m.",
                f"Update total length for bar '{bm}' to {round(d_total_len, 2)}m.",
                'error', zone, bar_bbox
            ))

    d_unit_wt = design_bar.get('unit_wt')
    w_unit_wt = _norm_float(drawing_bar.get('unit_wt_kg_m'))
    if d_unit_wt and w_unit_wt:
        diff = _pct_diff(d_unit_wt, w_unit_wt)
        if diff and diff > 2:
            issues.append(_issue(
                'Bar Unit Weight',
                f"{prefix}: Unit weight mismatch — design {round(d_unit_wt, 3)} kg/m, drawing {round(w_unit_wt, 3)} kg/m",
                f"Design input unit weight = {round(d_unit_wt, 3)} kg/m for '{bm}' ({comp}). Drawing schedule shows {round(w_unit_wt, 3)} kg/m.",
                f"Unit weight for bar '{bm}' should be {round(d_unit_wt, 3)} kg/m — check for typo in schedule.",
                'error', zone, bar_bbox
            ))

    # Total weight: compare design input (Excel formula value, authoritative) vs drawing schedule
    d_total_wt = design_bar.get('total_wt_kg')
    if all_design_bars and len(all_design_bars) > 1:
        vals = [b.get('total_wt_kg') for b in all_design_bars if b.get('total_wt_kg')]
        if vals:
            d_total_wt = sum(vals)
    w_total_wt = _norm_float(drawing_bar.get('total_wt_kg'))
    if d_total_wt and w_total_wt:
        diff = _pct_diff(d_total_wt, w_total_wt)
        if diff and diff > 5:
            issues.append(_issue(
                'Bar Weight',
                f"{prefix}: Total weight mismatch — design {d_total_wt:.1f}kg, drawing {w_total_wt:.1f}kg",
                f"Design input total weight = {d_total_wt:.1f}kg for bar '{bm}' ({comp}). Drawing schedule shows {w_total_wt:.1f}kg.",
                f"Recheck total weight for bar '{bm}' — likely a consequence of a count or length error.",
                'error', zone, bar_bbox
            ))

    # Reinforcement column check — verify the formatted text matches design intent.
    # reinf_secondary (from Excel font color) tells us which type is expected: spacing or count.
    reinf_secondary = design_bar.get('reinf_secondary')
    w_reinf_text    = drawing_bar.get('reinforcement_text') or ''
    if reinf_secondary and w_reinf_text:
        w_parsed = _parse_reinf_text(w_reinf_text)
        w_type   = w_parsed.get('secondary_type')
        w_val    = w_parsed.get('secondary_val')
        if w_type and w_type != reinf_secondary:
            exp_fmt  = 'φdia@spacing c/c' if reinf_secondary == 'spacing' else 'φdia × count NOS'
            draw_fmt = 'φdia@spacing c/c' if w_type == 'spacing' else 'φdia × count NOS'
            issues.append(_issue(
                'Reinforcement',
                f"{prefix}: Reinforcement column format wrong — shows {draw_fmt}, expected {exp_fmt}",
                f"Bar '{bm}' ({comp}): Excel colour indicates '{reinf_secondary}' as the secondary value "
                f"but the drawing reinforcement column uses '{w_type}' format ({w_reinf_text!r}).",
                f"Change the reinforcement entry for '{bm}' to use "
                f"{'@spacing c/c' if reinf_secondary == 'spacing' else '× count NOS'} format.",
                'error', zone, bar_bbox
            ))
        elif w_val is not None:
            if reinf_secondary == 'spacing' and d_spacing:
                diff = _pct_diff(d_spacing, w_val)
                if diff and diff > 5:
                    issues.append(_issue(
                        'Reinforcement',
                        f"{prefix}: Reinforcement column spacing mismatch — "
                        f"design {round(d_spacing)}mm, drawing text {round(w_val)}mm",
                        f"Bar '{bm}' ({comp}): reinforcement column shows {round(w_val)}mm c/c "
                        f"but design expects {round(d_spacing)}mm c/c ({w_reinf_text!r}).",
                        f"Update the reinforcement column spacing for '{bm}' to {round(d_spacing)}mm c/c.",
                        'error', zone, bar_bbox
                    ))
            elif reinf_secondary == 'count' and d_count and int(w_val) != d_count:
                issues.append(_issue(
                    'Reinforcement',
                    f"{prefix}: Reinforcement column count mismatch — "
                    f"design {d_count} NOS, drawing text {int(w_val)} NOS",
                    f"Bar '{bm}' ({comp}): reinforcement column shows {int(w_val)} NOS "
                    f"but design expects {d_count} NOS ({w_reinf_text!r}).",
                    f"Update the reinforcement column for '{bm}' to {d_count} NOS.",
                    'error', zone, bar_bbox
                ))

    # Bar shape dimension check.
    # Excel stores shape dims in metres; drawing schedule shows them in mm → convert × 1000.
    # Use best-match (sorted) pairing: for each Excel dim find closest drawing dim.
    # Extra drawing dims with no Excel counterpart are ignored (rule: only check Excel dims).
    d_shape_dims = design_bar.get('shape_dims')
    w_shape_dims = drawing_bar.get('shape_dimensions')
    if shape_dims_capable and d_shape_dims and w_shape_dims and isinstance(w_shape_dims, list):
        d_vals_mm = sorted(
            v * 1000
            for d in d_shape_dims
            for v in [_norm_float(d)]
            if v
        )
        w_vals_mm = sorted(
            v
            for w in w_shape_dims
            for v in [_norm_float(w)]
            if v
        )
        remaining_w = list(w_vals_mm)
        for d_mm in d_vals_mm:
            if not remaining_w:
                break
            # Correct "1-prefix misread": e.g. Claude returns 1300 when design expects 300,
            # caused by the trailing digit of a bar mark (f1, y1, i1 etc.) bleeding into the
            # adjacent shape sketch column. If stripping the leading "1" from a drawing value
            # lands within 3% of the design value, treat the corrected value as the match.
            corrected_w = [_strip_1prefix_misread(w, d_vals_mm) for w in remaining_w]
            closest_corrected = min(corrected_w, key=lambda w: abs(w - d_mm))
            closest_idx = corrected_w.index(closest_corrected)
            remaining_w.pop(closest_idx)
            diff = _pct_diff(d_mm, closest_corrected)
            # >55% diff means no drawing dim is plausibly the same segment as the design dim.
            # Most likely the design dim is an AutoCAD DIMENSION entity (unreadable by text
            # extraction) and the closest match is an unrelated annotation (hook length, etc.).
            # Real transcription errors are <30%; skip rather than generate a misleading alert.
            if diff and 2 < diff <= 55:
                issues.append(_issue(
                    'Bar Shape',
                    f"{prefix}: Bar shape dimension mismatch — design {d_mm:.0f}mm, drawing {closest_corrected:.0f}mm",
                    f"Bar '{bm}' ({comp}): design input has segment {d_mm:.0f}mm "
                    f"but closest value in drawing is {closest_corrected:.0f}mm.",
                    f"Correct the bar shape dimension for '{bm}' to {d_mm:.0f}mm.",
                    'error', zone, bar_bbox
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

def _check_sections(sections_from_text: list) -> list:
    """Check required section presence using pdfplumber-extracted text (authoritative)."""
    issues = []
    if not sections_from_text:
        # No text extraction result — skip rather than flag as configuration error
        return issues

    for entry in sections_from_text:
        if not entry.get('present'):
            name = entry.get('name', 'Unknown view')
            bbox = entry.get('bbox')
            issues.append(_issue(
                'Missing Views', f'Missing view: {name}',
                f'"{name}" was not found in the drawing (checked via PDF text extraction).',
                f'Add the "{name}" view to the drawing.',
                'error', 'default', bbox
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

def _check_notes_completeness(notes_completeness_from_text: list,
                               section_view_positions: dict = None) -> list:
    """Check required notes presence using pdfplumber text extraction (authoritative)."""
    issues = []
    if not notes_completeness_from_text:
        return issues

    found = {n.get('item'): n for n in notes_completeness_from_text}

    # Any concrete grade covers all three components (single-grade convention).
    concrete_keys = ('concrete_pile', 'concrete_pilecap', 'concrete_pier')
    any_concrete_found = any(
        found.get(k) and found[k].get('present') for k in concrete_keys
    )

    notes_bbox = _find_section_bbox('NOTES', [], section_view_positions)

    for item_key, missing_msg in REQUIRED_NOTES.items():
        entry = found.get(item_key)
        if not entry or not entry.get('present'):
            if item_key in concrete_keys and any_concrete_found:
                continue
            issues.append(_issue(
                'Notes', missing_msg,
                f'The note for "{item_key.replace("_", " ")}" was not found in the drawing text.',
                f'Add the required note for {item_key.replace("_", " ")}.',
                'error', 'notes', notes_bbox
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
        log.info('Label issue from Claude: %s', desc[:120])
        bbox = li.get('bbox') or _find_section_bbox(
            desc + ' ' + li.get('suggestion', ''), sections, section_view_positions
        )
        issues.append(_issue(
            'Label Errors', desc, desc,
            li.get('suggestion', 'Review and correct this label.'),
            'error', 'default', bbox
        ))
    return issues


# ── Cross-section bar count & quality ────────────────────────────────────────

def _xsec_bbox(section_name: str, drawing_data: dict):
    """Return a PDF-space bbox for a cross-section view.

    Prefers pdfplumber section_view_positions (PDF coordinates). Falls back to
    BBOX_FALLBACK['default']. Never uses DXF model-space bbox — those coordinates
    do not map to the PDF page coordinate system.
    """
    svp = drawing_data.get('section_view_positions') or {}
    needle = section_name.upper()   # e.g. "A-A"
    for label, pos in svp.items():
        if needle in label.upper() and pos and all(k in pos for k in ('x', 'y', 'w', 'h')):
            return pos
    return None   # triggers BBOX_FALLBACK['default'] in _bbox()


def _check_cross_sections(drawing_data: dict, design_data: dict) -> list:
    issues = []
    cross_checks  = drawing_data.get('cross_section_checks') or []
    erroneous_boxes = drawing_data.get('erroneous_boxes') or []
    schedule      = drawing_data.get('schedule') or {}
    num_piles     = int((design_data or {}).get('geometry', {}).get('pile_count') or 1)

    for cc in cross_checks:
        section_name    = cc.get('section_name') or '?'
        component       = (cc.get('component') or '').lower()
        bar_mark        = (cc.get('bar_mark') or '').lower().strip()
        visual_count    = cc.get('visual_count')
        is_bundle       = cc.get('is_bundle', False)
        spacing_uniform = cc.get('spacing_uniform', True)
        bbox            = _xsec_bbox(section_name, drawing_data)

        if visual_count is None:
            continue

        # Spacing — one issue per detected irregularity with location and type
        spacing_issues = cc.get('spacing_issues') or []
        # Backward compat: old Claude response sets spacing_uniform=False but no spacing_issues array
        if not spacing_uniform and not spacing_issues:
            spacing_issues = [{'type': 'uneven', 'location': '', 'description': 'Bars are not evenly distributed.'}]

        _TYPE_LABEL = {
            'clustering':  'bar clustering',
            'gap':         'gap between bars',
            'missing_bar': 'possible missing bar',
        }
        for si in spacing_issues:
            x, y, w, h = _bbox('default', bbox)
            si_type  = si.get('type', 'uneven')
            si_loc   = si.get('location', '')
            si_desc  = si.get('description', '')
            type_label = _TYPE_LABEL.get(si_type, si_type)
            loc_suffix = f" at {si_loc}" if si_loc else ''
            issues.append({
                'category':    'Section Spacing',
                'title':       f"Section {section_name}: bar '{bar_mark}' — {type_label}{loc_suffix}",
                'description': si_desc or f"In Section {section_name}, the '{bar_mark}' bars show {type_label}.",
                'suggestion':  (
                    f"Check bar '{bar_mark}' spacing in Section {section_name}{loc_suffix}. "
                    "Bars should be uniformly distributed."
                ),
                'severity':    'error',
                'page_num':    1,
                'x': x, 'y': y, 'width': w, 'height': h,
            })

        # Bar count — needs matching schedule row
        if not component or not bar_mark:
            continue
        drawing_bar = schedule.get(component, {}).get(bar_mark)
        if not drawing_bar:
            continue

        schedule_count = _norm_count(
            drawing_bar.get('count') or drawing_bar.get('count_text', ''))
        if not schedule_count:
            continue

        bundle_factor = 2 if is_bundle else _parse_bundle_factor(
            drawing_bar.get('reinforcement_text', ''))

        # Pile sections: divide total by pile count and bundle factor.
        # Pilecap/pier sections: divide only by bundle factor.
        divisor = (num_piles * bundle_factor) if component == 'pile' else bundle_factor
        if divisor < 1:
            divisor = 1
        expected = round(schedule_count / divisor)

        if visual_count != expected:
            breakdown = f"{schedule_count} total"
            if component == 'pile':
                breakdown += f" ÷ {num_piles} piles"
            if bundle_factor > 1:
                breakdown += f" ÷ {bundle_factor} (bundle)"
            x, y, w, h = _bbox('default', bbox)
            issues.append({
                'category':    'Section Bar Count',
                'title':       (
                    f"Section {section_name}: bar '{bar_mark}' count — "
                    f"drawn {visual_count}, expected {expected}"
                ),
                'description': (
                    f"In Section {section_name}, {visual_count} bar(s) are drawn for '{bar_mark}' ({component}). "
                    f"Expected {expected} ({breakdown})."
                ),
                'suggestion':  f"Verify bar count for '{bar_mark}' in Section {section_name} against the schedule.",
                'severity':    'error',
                'page_num':    1,
                'x': x, 'y': y, 'width': w, 'height': h,
            })

    for eb in (erroneous_boxes or []):
        desc = eb.get('description') or 'A stray rectangular outline is present on the drawing.'
        bbox = eb.get('bbox')
        x, y, w, h = _bbox('default', bbox)
        issues.append({
            'category':    'Drawing Quality',
            'title':       'Stray box / erroneous outline',
            'description': desc,
            'suggestion':  'Remove the stray outline, or label it if it is an intentional view boundary.',
            'severity':    'error',
            'page_num':    1,
            'x': x, 'y': y, 'width': w, 'height': h,
        })

    return issues


# ── Cut-mark cross-reference ──────────────────────────────────────────────────

# Words that identify a tabular element rather than a structural drawing view.
# Cut-mark arrows never appear inside tables — any entry whose found_on_view
# contains one of these is a text-reference false positive.
_TABLE_WORDS = (
    'lap length', 'schedule of reinforcement', 'table-1', 'table 1',
    'referenced in', 'notes', 'annotation',
)


def _check_cut_mark_references(drawing_data: dict) -> list:
    issues = []
    for item in (drawing_data.get('missing_referenced_sections') or []):
        missing  = item.get('missing_section', '?')
        found_on = item.get('found_on_view', '?')
        # Skip if Claude sourced the "cut mark" from a table or text annotation
        # rather than a structural drawing view — those are text references, not arrows.
        if any(w in found_on.lower() for w in _TABLE_WORDS):
            continue
        bbox     = item.get('bbox')
        x, y, w, h = _bbox('default', bbox)
        issues.append({
            'category':    'Missing Views',
            'title':       f'Missing section view: {missing}',
            'description': (
                f'Cut marks for "{missing}" are shown on "{found_on}" '
                f'but the section view "{missing}" is not drawn anywhere in the drawing.'
            ),
            'suggestion':  (
                f'Add the "{missing}" view to the drawing, or remove the cut marks '
                f'if this section is not required.'
            ),
            'severity':    'error',
            'page_num':    1,
            'x': x, 'y': y, 'width': w, 'height': h,
        })
    return issues


# ── Unlabeled views ───────────────────────────────────────────────────────────

def _check_unlabeled_views(drawing_data: dict) -> list:
    issues = []
    for item in (drawing_data.get('unlabeled_views') or []):
        desc    = item.get('description') or 'Section/plan view without a title label'
        bbox    = item.get('bbox')
        x, y, w, h = _bbox('default', bbox)
        issues.append({
            'category':    'Missing Views',
            'title':       f'Unlabeled view: {desc}',
            'description': (
                f'{desc}. Every drawn view (section, plan, elevation) '
                'must carry a title label such as "SECTION X-X" or "PLAN OF...".'
            ),
            'suggestion':  'Add a title label to this view.',
            'severity':    'error',
            'page_num':    1,
            'x': x, 'y': y, 'width': w, 'height': h,
        })
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
            'Dimension Errors', desc, desc,
            di.get('suggestion', 'Add the missing dimension.'),
            'error', 'default', bbox
        ))
    return issues


# ── Geometric dimension comparison (Tier 2 spatial routing) ──────────────────

def _compare_geometry_dims(drawing_data: dict, design_data: dict,
                            profile=None) -> list:
    """
    Compare geometry_from_drawing (DXF spatial classification) against design_data['geometry'].
    Uses profile.geometry_checks to know which params to check and their tolerances.
    """
    if profile is None:
        profile = PPP_PROFILE

    geo_drawing = drawing_data.get('geometry_from_drawing') or {}
    geo_design  = (design_data or {}).get('geometry') or {}

    if not geo_drawing or not geo_design:
        return []

    checks = list(profile.geometry_checks)

    # A single section view shows only one plan direction of the pier (length along
    # traffic, or width across traffic) — resolve 'pier_plan_dim' to whichever design
    # dimension it's numerically closer to before running the declarative checks below.
    # geo_drawing values are lists (a real sheet may show the same physical component
    # via more than one section cut) — use the first reading to pick the direction.
    pier_dims = geo_drawing.get('pier_plan_dim')
    if pier_dims:
        pl, pw = geo_design.get('pier_length'), geo_design.get('pier_width')
        candidates = [(k, v) for k, v in (('pier_length', pl), ('pier_width', pw)) if v is not None]
        if candidates:
            closest_key = min(candidates, key=lambda kv: abs(pier_dims[0]['val_mm'] - kv[1] * 1000.0))[0]
            checks.append(('pier_plan_dim', closest_key, 5.0, 'Pier plan dimension', 'm'))

    issues = []
    for param, design_key, tol_pct, label, design_unit in checks:
        drawn_list = geo_drawing.get(param)
        if not drawn_list:
            continue

        design_val = geo_design.get(design_key)
        if design_val is None:
            continue

        design_mm = design_val * 1000.0 if design_unit == 'm' else float(design_val)
        multi = len(drawn_list) > 1

        for drawn in drawn_list:
            drawn_mm = drawn['val_mm']
            diff_pct = abs(drawn_mm - design_mm) / design_mm * 100 if design_mm else 0
            if diff_pct <= tol_pct:
                continue

            x, y = drawn.get('x_pct', 50), drawn.get('y_pct', 50)
            suffix = f' (view near x={x:.0f}%)' if multi else ''
            issues.append({
                'category':    'Geometric Dimensions',
                'title':       f'{label}: drawn {drawn_mm:.0f}mm ≠ design {design_mm:.0f}mm{suffix}',
                'description': (
                    f'{label} drawn as {drawn_mm:.0f}mm but design specifies '
                    f'{design_mm:.0f}mm ({design_val}{design_unit}). '
                    f'Difference: {diff_pct:.1f}% (tolerance {tol_pct}%).'
                ),
                'suggestion':  f'Correct {label.lower()} to {design_mm:.0f}mm.',
                'severity':    'error',
                'page_num':    1,
                'x': x, 'y': y, 'width': 5.0, 'height': 2.0,
            })

        # The same physical component is measured by more than one section cut —
        # those readings should agree with each other, independent of the design
        # comparison above. Disagreement is a genuine drafting inconsistency.
        if multi:
            vals = [d['val_mm'] for d in drawn_list]
            if (max(vals) - min(vals)) / max(vals) * 100 > tol_pct:
                x, y = drawn_list[0].get('x_pct', 50), drawn_list[0].get('y_pct', 50)
                issues.append({
                    'category':    'Geometric Dimensions',
                    'title':       f'{label}: inconsistent between drawing views',
                    'description': (
                        f'{label} measures differently across section views in the same '
                        f'drawing: {", ".join(f"{v:.0f}mm" for v in vals)}.'
                    ),
                    'suggestion':  f'Confirm {label.lower()} is drawn consistently across all section views.',
                    'severity':    'error',
                    'page_num':    1,
                    'x': x, 'y': y, 'width': 5.0, 'height': 2.0,
                })
    return issues


def _check_bar_mark_callouts(drawing_data: dict, design_data: dict) -> list:
    """
    Check that every bar mark expected from the design schedule appears as a
    MULTILEADER callout in the drawing. Flags marks that are in the schedule
    but have no visible leader annotation in any section view.
    """
    callouts = {c['bar_mark'] for c in (drawing_data.get('multileader_callouts') or [])}
    if not callouts:
        return []   # no callout data — drawing may not use MULTILEADER; skip silently

    schedule = drawing_data.get('schedule') or {}
    design_schedule = {}
    if design_data:
        for comp in ('pilecap_bbs', 'pile_bbs', 'pier_bbs'):
            for bm in (design_data.get(comp) or {}):
                design_schedule[bm] = comp.replace('_bbs', '')

    issues = []
    for bm in sorted(design_schedule):
        if bm not in schedule:
            continue   # not in drawing schedule — flagged elsewhere
        if bm not in callouts:
            issues.append({
                'category':    'Bar Mark Callouts',
                'title':       f"Bar mark '{bm}' not called out in section view",
                'description': (
                    f"Bar mark '{bm}' appears in the reinforcement schedule but has no "
                    f"leader annotation in any section view. Engineers must be able to "
                    f"trace each bar mark from the schedule to its location in the drawing."
                ),
                'suggestion':  (
                    f"Add a MULTILEADER callout for bar mark '{bm}' pointing to the "
                    f"bar in the relevant section view."
                ),
                'severity':    'error',
                'page_num':    1,
                'x': 50.0, 'y': 50.0, 'width': 5.0, 'height': 2.0,
            })
    return issues
