"""
DXF-based drawing data extractor for CASAD ED Checker.
Reads an AutoCAD DXF file (exported via File > Save As > AutoCAD DXF) and produces the
same drawing_data dict as pdf_extractor.extract_from_drawing(), giving exact text values
with no OCR or PDF encoding errors.

Called from __init__.run_check() when the engineer uploads a DXF alongside the PDF.
The PDF is still required for display in the review UI; the DXF is used only for data
extraction.
"""
import io
import os
import re
import math
import logging
import tempfile

log = logging.getLogger(__name__)

# Mirror constants from pdf_extractor — kept in sync manually
_REQUIRED_PPP_SECTIONS = [
    ('SECTION A-A FOR PILE',           ['A-A FOR PILE']),
    ('SECTION Z-Z (PILE)',             ['Z-Z']),
    ('SECTION A-A FOR PILECAP & PIER', ['A-A FOR PILECAP']),
    ('SECTION B-B FOR PILECAP & PIER', ['B-B FOR PILECAP']),
    ('PLAN OF PILECAP',                ['PLAN OF PILECAP']),
    ('REINFORCEMENT PLAN OF PILECAP',  ['REINFORCEMENT PLAN']),
    ('TABLE-1',                        ['TABLE-1', 'TABLE 1']),
    ('LAP LENGTH TABLE',               ['LAP LENGTH']),
    ('SCHEDULE OF REINFORCEMENT',      ['SCHEDULE OF REINFORCEMENT']),
]

_NOTE_KEYWORDS = {
    'pile_length':      ['PILE LENGTH', 'LENGTH OF PILE'],
    'pile_fixity':      ['FIXITY', 'FIX. LENGTH', 'FIX LENGTH', 'FIXATION'],
    'pile_diameter':    ['PILE DIA', 'DIAMETER OF PILE', 'PILE DIAMETER'],
    'concrete_pile':    ['M30', 'M35', 'M40', 'M45', 'M50'],
    'concrete_pilecap': ['M30', 'M35', 'M40', 'M45', 'M50'],
    'concrete_pier':    ['M30', 'M35', 'M40', 'M45', 'M50'],
    'steel_grade':      ['FE415', 'FE500', 'FE550', 'FE 415', 'FE 500', 'FE 550', 'HYSD', 'TMT'],
    'irc_code_ref':     ['IRC:', 'IRC-', 'IRC '],
}

# Bar mark → component mapping (replaces header-based component detection)
# Based on CASAD PPP drawing convention — works without finding component header rows.
_BAR_MARK_COMP = {
    'x': 'pile', 'y': 'pile', 'y1': 'pile', 'z': 'pile',
    'a': 'pilecap', 'b': 'pilecap', 'c': 'pilecap', 'd': 'pilecap',
    'e': 'pilecap', 'f': 'pilecap', 'f1': 'pilecap',
    'g': 'pier', 'h': 'pier', 'h1': 'pier', 'h2': 'pier',
    'i': 'pier', 'i1': 'pier',
    'j': 'pier', 'j1': 'pier',
    'k': 'pier', 'k1': 'pier',
}

# Schedule column header keywords → internal field name.
# CASAD schedule column headers span multiple Y-rows (e.g. "TOTAL" on one line,
# "LENGTH IN" on next, "METER" on next). _build_col_map receives all sub-rows
# and concatenates text per X-band before matching.
_COL_KEYWORDS = {
    'bar_mark':       ['BAR MARK', 'BAR\nMARK', 'MARK', 'MK.', 'MK'],
    'bar_dia_mm':     ['DIA', 'Ø', 'φ', 'PHI', 'DIAMETER'],
    'spacing_mm':     ['SPACING', 'C/C', 'PITCH', 'SPC'],
    'count':          ['NOS', 'NO.', 'NUMBER', 'COUNT', 'NOS.'],
    'length_m':       ['LENGTH OF BAR', 'LENGTH OF', 'BAR LENGTH', 'LENGTH (M)', 'LENGTH(M)', 'LENGTH'],
    'total_length_m': ['TOTAL LENGTH', 'TOTAL LEN', 'TOT. LEN'],
    'unit_wt_kg_m':   ['UNIT WT', 'UNIT WEIGHT', 'WT./M', 'KG/M', 'UNIT W', 'WEIGHT/R', 'R. MT.', 'WEIGHT/'],
    'total_wt_kg':    ['TOTAL WT', 'TOTAL WEIGHT', 'TOTAL WGT', 'TOT. WT'],
}

# Known valid bar diameters in mm
_VALID_DIA = {8, 10, 12, 16, 20, 25, 32}

# Single-letter bar marks used in CASAD PPP drawings
_KNOWN_MARKS = set('abcdefghijklmnopqrstuvwxyz') | {
    'a1', 'b1', 'c1', 'd1', 'e1', 'f1', 'g1', 'i1', 'j1', 'k1', 'x1', 'y1', 'z1',
}

# Section letter → likely component
_SECTION_LETTER_COMP = {
    'Z': 'pile',
    'A': 'pile',   # SECTION A-A FOR PILE or FOR PILECAP (resolved by label text)
    'B': 'pilecap',
    'C': 'pier',
    'D': 'pier',
}

TRIGGER_WORDS = {'SECTION', 'TABLE-1', 'TABLE 1', 'LAP', 'NOTES', 'DETAIL', 'PLAN', 'REINFORCEMENT'}


# ── Public entry point ────────────────────────────────────────────────────────

def extract_from_dxf(dxf_bytes: bytes) -> dict:
    """
    Parse an AutoCAD DXF file and return drawing_data compatible with comparator.compare().
    Produces exact values — no OCR, no vision API required.
    """
    try:
        import ezdxf
    except ImportError:
        log.error('ezdxf not installed. Run: pip install ezdxf>=1.3.0')
        return _empty()

    # ezdxf.read(BytesIO) fails on AutoCAD DXF files that contain binary data
    # (embedded images, binary chunks in MTEXT). ezdxf.readfile() handles encoding
    # detection correctly. Write to a temp file and use readfile() instead.
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
            tmp.write(dxf_bytes)
            tmp_path = tmp.name
        doc = ezdxf.readfile(tmp_path)
    except Exception as e:
        log.error('DXF parse failed: %s', e, exc_info=True)
        return _empty()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    msp     = doc.modelspace()
    extents = _get_extents(doc)
    all_text = _collect_text(msp)

    log.info('DXF: %d text entities, extents=(%.1f,%.1f)–(%.1f,%.1f)',
             len(all_text), *extents)

    schedule            = _extract_schedule(msp, all_text, extents)
    title_block         = _extract_title_block(doc, msp, all_text, extents)
    notes               = _extract_notes(all_text, extents)
    dim_data            = _extract_dimensions(msp, extents)
    table_1             = _extract_table1(all_text, extents)
    sv_pos, cut_letters = _extract_section_info(all_text, extents)
    xsec                = _count_cross_section_bars(msp, all_text, schedule, extents)

    # Supplement notes with DIMENSION-derived values when text extraction missed them.
    # DIMENSION entities give exact geometric measurements with no OCR error.
    if dim_data.get('pile_length_mm') and notes.get('pile_length_m') is None:
        notes['pile_length_m'] = round(dim_data['pile_length_mm'] / 1000.0, 3)
        log.info('Notes: pile_length_m set from DIMENSION entity: %.3fm', notes['pile_length_m'])
    if dim_data.get('pile_dia_mm') and notes.get('pile_dia_m') is None:
        notes['pile_dia_m'] = round(dim_data['pile_dia_mm'] / 1000.0, 3)
        log.info('Notes: pile_dia_m set from DIMENSION entity: %.3fm', notes['pile_dia_m'])

    log.info('DXF extraction done: comps=%s title_fields=%d sections=%d cuts=%s xsec=%d',
             list(schedule.keys()),
             sum(1 for v in title_block.values() if v),
             len(sv_pos), sorted(cut_letters), len(xsec))

    return {
        'schedule':                     schedule,
        'title_block':                  title_block,
        'notes':                        notes,
        'dim_data':                     dim_data,
        'table_1':                      table_1,
        'cross_section_checks':         xsec,
        'section_view_positions':       sv_pos,
        'cut_letters':                  cut_letters,
        # Filled in by pdfplumber merge in __init__.py (PDF coords for marker placement):
        'schedule_section_positions':   {},
        'schedule_section_bboxes':      {},
        # Not available from DXF text alone (Phase 2):
        'label_issues':                 [],
        'dimension_issues':             [],
        'unlabeled_views':              [],
        'erroneous_boxes':              [],
        # Computed in __init__.py after pdfplumber merge:
        'missing_referenced_sections':  [],
        # Completeness checks — derived from DXF text:
        'sections_from_text':           _check_required_sections(sv_pos),
        'notes_completeness_from_text': _check_notes_completeness(all_text),
        'raw_text':                     [t['text'] for t in all_text],
    }


def _empty() -> dict:
    return {
        'schedule': {}, 'title_block': {}, 'notes': {}, 'table_1': [],
        'cross_section_checks': [], 'section_view_positions': {}, 'cut_letters': set(),
        'schedule_section_positions': {}, 'schedule_section_bboxes': {},
        'label_issues': [], 'dimension_issues': [], 'unlabeled_views': [],
        'erroneous_boxes': [], 'missing_referenced_sections': [],
        'sections_from_text': [], 'notes_completeness_from_text': [], 'raw_text': [],
    }


# ── Geometry helpers ──────────────────────────────────────────────────────────

def _get_extents(doc) -> tuple:
    """Return (x_min, y_min, x_max, y_max) from DXF header."""
    try:
        mn = doc.header.get('$EXTMIN', (0, 0, 0))
        mx = doc.header.get('$EXTMAX', (1000, 700, 0))
        return float(mn[0]), float(mn[1]), float(mx[0]), float(mx[1])
    except Exception:
        return 0.0, 0.0, 1000.0, 700.0


def _to_bbox(x0, y0, x1, y1, extents: tuple) -> dict:
    """Convert model-space rectangle to PDF-style percentage bbox {x,y,w,h}.
    DXF Y is bottom-up; PDF Y is top-down — we flip Y here.
    """
    xn, yn, xx, yx = extents
    dx = (xx - xn) or 1.0
    dy = (yx - yn) or 1.0
    left  = min(x0, x1)
    right = max(x0, x1)
    bot   = min(y0, y1)
    top   = max(y0, y1)
    return {
        'x': round((left - xn) / dx * 100, 2),
        'y': round((yx - top)  / dy * 100, 2),   # flip: PDF y=0 is page top
        'w': round((right - left) / dx * 100, 2),
        'h': round((top - bot)    / dy * 100, 2),
    }


def _pos_to_pct(x, y, extents: tuple) -> tuple:
    """Convert model-space point to (x_pct, y_pct) in PDF-style percentages."""
    xn, yn, xx, yx = extents
    dx = (xx - xn) or 1.0
    dy = (yx - yn) or 1.0
    return round((x - xn) / dx * 100, 2), round((yx - y) / dy * 100, 2)


# ── Text collection ───────────────────────────────────────────────────────────

def _collect_text(msp) -> list:
    """Return [{text, x, y}] for all TEXT and MTEXT entities in model space."""
    result = []
    for e in msp.query('TEXT'):
        try:
            text = _strip_text_codes((e.dxf.text or '').strip())
            if text:
                p = e.dxf.insert
                result.append({'text': text, 'x': float(p.x), 'y': float(p.y)})
        except Exception:
            pass
    for e in msp.query('MTEXT'):
        try:
            try:
                text = e.plain_mtext().strip()
            except AttributeError:
                text = _strip_mtext_codes(e.dxf.text or '')
            if text:
                p = e.dxf.insert
                result.append({'text': text, 'x': float(p.x), 'y': float(p.y)})
        except Exception:
            pass
    return result


def _strip_text_codes(text: str) -> str:
    """Strip AutoCAD legacy %%-escape codes from TEXT entity strings.
    %%U and %%O are underline/overline toggles — invisible formatting.
    %%D/%%P/%%C are special characters — replace with readable equivalents.
    """
    text = re.sub(r'%%[uUoO]', '', text)        # underline / overline toggles
    text = text.replace('%%d', '°').replace('%%D', '°')
    text = text.replace('%%p', '±').replace('%%P', '±')
    text = text.replace('%%c', 'Ø').replace('%%C', 'Ø')
    text = re.sub(r'%%\d{3}', '', text)          # numeric char codes
    return text.strip()


def _strip_mtext_codes(text: str) -> str:
    """Remove AutoCAD MTEXT inline formatting codes."""
    text = re.sub(r'\\[PpLlOoKkCcFfAaQqHhWwBbIiTtSs][^;]*;', '', text)
    text = re.sub(r'\{[^}]*\}', '', text)
    text = text.replace('\\~', ' ').replace('\\P', '\n').replace('\\p', '\n')
    return text.strip()


# ── Row grouping helper ───────────────────────────────────────────────────────

def _group_rows(text_list: list, tol_frac: float = 0.005, extents: tuple = None) -> list:
    """
    Group [{text,x,y}] entries into rows based on Y proximity.
    tol_frac: tolerance as a fraction of drawing height (default 0.5%).
    Returns list of rows; each row is a list of dicts sorted by x.
    Rows are ordered top-to-bottom (descending Y in DXF = ascending screen-top).
    """
    if not text_list:
        return []
    if extents:
        dy = (extents[3] - extents[1]) or 1.0
        tol = dy * tol_frac
    else:
        ys = [t['y'] for t in text_list]
        tol = (max(ys) - min(ys)) * tol_frac if len(ys) > 1 else 2.0

    sorted_items = sorted(text_list, key=lambda t: t['y'], reverse=True)
    rows = []
    current_row = [sorted_items[0]]
    current_y = sorted_items[0]['y']

    for item in sorted_items[1:]:
        if abs(item['y'] - current_y) <= tol:
            current_row.append(item)
        else:
            rows.append(sorted(current_row, key=lambda t: t['x']))
            current_row = [item]
            current_y = item['y']
    if current_row:
        rows.append(sorted(current_row, key=lambda t: t['x']))

    return rows  # top-to-bottom order (highest DXF Y first)


# ── Schedule extraction ───────────────────────────────────────────────────────

def _get_vertical_line_x_positions(msp, sched_x_min: float, x_max: float, dh: float) -> list:
    """
    Return sorted list of X positions of significant vertical LINE entities in the schedule area.
    Vertical lines that span >10% of drawing height are likely table column dividers.
    These are used as more reliable column anchors than keyword-matched header text X positions.
    """
    candidates = []
    for e in msp.query('LINE'):
        try:
            sx, sy = float(e.dxf.start.x), float(e.dxf.start.y)
            ex, ey = float(e.dxf.end.x), float(e.dxf.end.y)
            # Vertical: X coords nearly equal, Y span significant (> 10% drawing height)
            if abs(sx - ex) < 10 and abs(sy - ey) > dh * 0.10:
                if sched_x_min <= sx <= x_max:
                    candidates.append(round(sx, 0))
        except Exception:
            pass
    # Deduplicate with small tolerance (±100 units)
    if not candidates:
        return []
    candidates.sort()
    deduped = [candidates[0]]
    for x in candidates[1:]:
        if x - deduped[-1] > 100:
            deduped.append(x)
    return deduped


def _aggregate_bar_rows(bar_rows: list, col_map: dict, bm: str) -> dict | None:
    """
    Aggregate data from all rows in a bar's range into one bar dict.

    CASAD confinement bars (y, y1, i, etc.) have multiple sub-rows — one per
    confinement zone — each with its own NOS, TOTAL_LEN, TOTAL_WT values but
    the same DIA, BAR_LEN, UNIT_WT. The bar mark label sits between the zones.

    Count strategy: collect ALL cells assigned to the NOS column across all rows,
    then sum only the '=N' cells (per-zone result column). If no '=N' cells, sum
    formula/direct cells. '=N' cells are authoritative zone totals; their sum gives
    the grand total across all zones. Collecting all NOS cells (not just one per
    row) is critical — '=N' and formula cells often appear in the same Y-group.

    For DIA, BAR_LEN, UNIT_WT: take first parseable value (same across zones).
    For TOTAL_LEN, TOTAL_WT: sum all values across zone rows.
    """
    if not bar_rows or not col_map:
        return None

    # Only use real column keys (no scratch suffixes) for proximity test
    real_col_map = {k: v for k, v in col_map.items() if not k.endswith('_assigned_x')}
    if not real_col_map:
        return None

    # Detect x-offset between this bar's block and the primary column map.
    # CASAD drawings sometimes have TWO schedule tables side-by-side (e.g. two
    # pier groups) at different horizontal positions. The column map is built from
    # the first table's header; a second table is shifted by a fixed offset.
    # Find the bar mark cell in bar_rows and compute the shift.
    x_offset = 0.0
    bm_col_x = real_col_map.get('bar_mark')
    if bm_col_x is not None:
        for row in bar_rows:
            for cell in row:
                if cell['text'].strip().strip("'\"").lower() == bm:
                    x_offset = cell['x'] - bm_col_x
                    break
            else:
                continue
            break
    if abs(x_offset) > 1000:
        log.debug('Bar %r: applying x_offset=%.0f to column map', bm, x_offset)
        real_col_map = {k: v + x_offset for k, v in real_col_map.items()}

    x_span = max(real_col_map.values()) - min(real_col_map.values()) if len(real_col_map) > 1 else 100.0
    x_tol = x_span * 0.20

    # Accumulate ALL cells by column across all rows
    col_cells: dict[str, list[str]] = {}
    for row in bar_rows:
        for cell in row:
            nearest = min(real_col_map.items(), key=lambda kv: abs(kv[1] - cell['x']))
            field, fx = nearest
            if abs(fx - cell['x']) < x_tol:
                col_cells.setdefault(field, []).append(cell['text'])

    # NOS column — three tiers of count cells:
    #   nos_eq:      cells starting with '=' (e.g. '=84') — authoritative zone totals
    #   nos_formula: cells with a multiplication expression (e.g. '21x4') — formula totals
    #   nos_bare:    plain integers (e.g. '21') — per-zone counts
    # Priority: nos_eq → nos_formula → sum(nos_bare).
    # When a formula total exists alongside a per-zone bare count, the formula
    # already captures the total; adding the bare count would double-sum.
    nos_eq: list[int] = []
    nos_formula: list[int] = []
    nos_bare: list[int] = []
    _mul_re = re.compile(r'[×xX*]')
    for text in col_cells.get('count', []):
        s = text.strip()
        if s.startswith('='):
            v = _safe_float(s[1:].strip())
            if v is not None:
                nos_eq.append(int(v))
        elif _mul_re.search(s) and '=' not in s:
            v = _parse_count(s)
            if v is not None:
                nos_formula.append(v)
        else:
            v = _parse_count(s)
            if v is not None:
                nos_bare.append(v)

    count_val: int | None = None
    if nos_eq:
        count_val = sum(nos_eq)
    elif nos_formula:
        # One formula per zone — sum them (rare: usually a single formula covers all zones)
        count_val = sum(nos_formula)
    elif nos_bare:
        count_val = sum(nos_bare)

    # Scalar fields: first parseable value
    dia = next((x for x in (_parse_dia(t) for t in col_cells.get('bar_dia_mm', [])) if x is not None), None)
    length_m = next((x for x in (_safe_float(t) for t in col_cells.get('length_m', [])) if x is not None), None)
    unit_wt = next((x for x in (_safe_float(t) for t in col_cells.get('unit_wt_kg_m', [])) if x is not None), None)
    spacing = next((x for x in (_parse_spacing(t) for t in col_cells.get('spacing_mm', [])) if x is not None), None)

    # Summable fields: sum all parseable values across zones
    tot_len_vals = [x for x in (_safe_float(t) for t in col_cells.get('total_length_m', [])) if x is not None]
    tot_wt_vals = [x for x in (_safe_float(t) for t in col_cells.get('total_wt_kg', [])) if x is not None]
    total_length_m = sum(tot_len_vals) if tot_len_vals else None
    total_wt_kg = sum(tot_wt_vals) if tot_wt_vals else None

    # Consistency check: if count came from formula (NxM) but tot_len and bar_length
    # imply a different count, the formula is cross-pier annotation — fall back to bare.
    # Example: i1 shows bare 21 + formula '21x6=126', but tot_len=172.2 = 21×8.2,
    # not 126×8.2=1033.2.  The formula annotates the grand total across 6 pier spans.
    if (count_val is not None and nos_formula and nos_bare and
            not nos_eq and length_m and total_length_m):
        expected_from_formula = count_val * length_m
        expected_from_bare = sum(nos_bare) * length_m
        tol = total_length_m * 0.05
        formula_ok = abs(expected_from_formula - total_length_m) <= tol
        bare_ok    = abs(expected_from_bare    - total_length_m) <= tol
        if not formula_ok and bare_ok:
            log.debug(
                'Bar %r: formula count %d inconsistent with tot_len %.2f; '
                'using bare count %d', bm, count_val, total_length_m, sum(nos_bare))
            count_val = sum(nos_bare)

    if dia is None and length_m is None and count_val is None:
        return None

    return {
        'bar_mark':        bm,
        'component':       None,
        'reinforcement_text': None,
        'bar_dia_mm':      dia,
        'spacing_mm':      spacing,
        'count_text':      str(count_val) if count_val is not None else '',
        'count':           count_val,
        'length_m':        length_m,
        'total_length_m':  total_length_m,
        'unit_wt_kg_m':    unit_wt,
        'total_wt_kg':     total_wt_kg,
        'shape_dimensions': None,
        'from_dxf':        True,
    }


def _extract_schedule(msp, all_text: list, extents: tuple) -> dict:
    """Extract reinforcement schedule from DXF text entities."""
    x_min, y_min, x_max, y_max = extents
    dw = x_max - x_min
    dh = y_max - y_min

    # The schedule sits in roughly the middle third of CASAD drawings.
    # Previous threshold of 50% cut through the column headers (DIA/NOS/LENGTH
    # at 45-49%, bar marks at 38%). Use 30% to capture the full schedule.
    sched_x_min = x_min + dw * 0.30
    sched_text = [t for t in all_text if t['x'] >= sched_x_min]

    if not sched_text:
        log.warning('No text found in schedule area (x >= %.1f)', sched_x_min)
        return {}

    log.info('Schedule area: %d text entities at x >= %.1f (%.0f%% from left)',
             len(sched_text), sched_x_min, (sched_x_min - x_min) / dw * 100)

    rows = _group_rows(sched_text, tol_frac=0.004, extents=extents)
    if not rows:
        return {}

    # Find the column header row: must contain at least 2 of DIA / NOS / LENGTH.
    header_idx = None
    for idx, row in enumerate(rows):
        row_text = ' '.join(t['text'] for t in row).upper()
        if sum(1 for k in ('DIA', 'NOS', 'LENGTH') if k in row_text) >= 2:
            header_idx = idx
            break

    if header_idx is None:
        log.warning('Schedule column header row not found')
        return {}

    # CASAD schedule headers span multiple Y-lines. Collect all sub-rows
    # within 1.2% of drawing height of the identified header row.
    header_y = rows[header_idx][0]['y']
    header_sub_rows = [row for row in rows if abs(row[0]['y'] - header_y) < dh * 0.012]

    col_map = _build_col_map(header_sub_rows)
    log.info('Schedule column map: %s', {k: f'x≈{v:.0f}' for k, v in col_map.items()})

    if not col_map:
        log.warning('Column map empty — cannot parse schedule rows')
        return {}

    data_rows = rows[header_idx + 1:]
    if not data_rows:
        return {}

    # Pass 1 — locate bar mark rows.
    # CASAD draws the bar mark label ('x', 'y', 'a'…) as a quoted TEXT entity
    # that may appear before, after, or between the data rows for that bar.
    bar_positions: list[tuple[int, str]] = []  # (row_index_in_data_rows, bm)
    for idx, row in enumerate(data_rows):
        for cell in row:
            bm = cell['text'].strip().strip("'\"").lower()
            if bm in _KNOWN_MARKS:
                bar_positions.append((idx, bm))
                break  # at most one bar mark per row

    if not bar_positions:
        log.warning('No known bar marks found in schedule')
        return {}

    # Pass 2 — for each bar mark, assign rows in its range (midpoint between
    # adjacent bar marks) and aggregate all data found there.
    schedule: dict = {}
    n = len(data_rows)

    for pos_i, (bm_row_idx, bm) in enumerate(bar_positions):
        comp = _BAR_MARK_COMP.get(bm)
        if comp is None:
            log.debug('Unknown bar mark %r — skipping', bm)
            continue

        # Range start: midpoint between previous bar mark row and this one.
        if pos_i == 0:
            range_start = 0
        else:
            prev_idx = bar_positions[pos_i - 1][0]
            range_start = (prev_idx + bm_row_idx) // 2 + 1

        # Range end: midpoint between this bar mark row and the next.
        if pos_i == len(bar_positions) - 1:
            range_end = n
        else:
            next_idx = bar_positions[pos_i + 1][0]
            range_end = (bm_row_idx + next_idx) // 2 + 1

        bar_rows = data_rows[range_start:range_end]
        bar_data = _aggregate_bar_rows(bar_rows, col_map, bm)
        if bar_data is None:
            continue

        bar_data['bar_mark'] = bm
        comp_dict = schedule.setdefault(comp, {})
        if bm in comp_dict:
            # Bar mark seen again — second schedule block for a different pier variant.
            # The anchor-based aggregation already captures multi-zone bars (y, y1, i…)
            # within one occurrence, so any second appearance is a genuinely separate
            # block and should not double-count into the primary schedule.
            log.debug('Bar mark %r already recorded — skipping second-block occurrence', bm)
        else:
            comp_dict[bm] = bar_data

    log.info('Schedule parsed: %s', {c: list(b.keys()) for c, b in schedule.items()})
    return schedule


def _build_col_map(header_rows: list) -> dict:
    """
    Map column field name → x-center position from one or more header rows.
    header_rows: list of row-lists (each row is [{text, x, y}]).

    CASAD schedule headers span multiple Y-lines, so the caller passes ALL
    sub-rows in the header band. Text cells at the same X across multiple rows
    are concatenated to form the full column header string before keyword matching.
    """
    # Flatten all cells across all header rows
    all_cells = [cell for row in header_rows for cell in row]
    if not all_cells:
        return {}

    x_vals = [c['x'] for c in all_cells]
    x_span = max(x_vals) - min(x_vals) if len(x_vals) > 1 else 1.0
    # Group cells into X-bands (one band = one column).
    # Tolerance: 2% of the header X-span keeps tightly-spaced columns separate.
    x_tol = max(x_span * 0.02, 50)

    col_groups = {}  # x_center → list of text strings
    for cell in sorted(all_cells, key=lambda c: c['x']):
        placed = False
        for x_center in list(col_groups.keys()):
            if abs(cell['x'] - x_center) < x_tol:
                col_groups[x_center].append(cell['text'].upper().strip())
                placed = True
                break
        if not placed:
            col_groups[cell['x']] = [cell['text'].upper().strip()]

    # For each column band, concatenate all text parts and match against keywords.
    col_map = {}
    used_fields = set()
    for x_center in sorted(col_groups.keys()):
        full_col_text = ' '.join(col_groups[x_center])
        for field, keywords in _COL_KEYWORDS.items():
            if field in used_fields:
                continue
            for kw in sorted(keywords, key=len, reverse=True):
                if _kw_in_col(kw, full_col_text):
                    col_map[field] = x_center
                    used_fields.add(field)
                    break

    return col_map


def _kw_in_col(kw: str, col_text: str) -> bool:
    """
    Check whether a column keyword matches a column's concatenated header text.
    Substring match covers single-word keywords and exact-phrase headers.
    Word-set match handles multi-word keywords whose parts appear in different
    Y-rows of the header (and so arrive in X-sorted, not reading, order).
    """
    kw_u = kw.upper()
    col_u = col_text.upper()
    if kw_u in col_u:
        return True
    words = kw_u.split()
    return len(words) > 1 and all(w in col_u for w in words)


def _parse_schedule_row(row: list, col_map: dict) -> dict | None:
    """Parse a single schedule row into a bar data dict."""
    if not row:
        return None

    # Assign each cell to a column by nearest x position
    cell_map = {}   # field → text
    for cell in row:
        if not col_map:
            # No column map — use positional assignment: first col = bar mark
            break
        nearest_field = min(col_map.items(), key=lambda kv: abs(kv[1] - cell['x']))
        field, field_x = nearest_field
        # Only assign if within 15% of the column x (avoids cross-contamination)
        x_span = max(col_map.values()) - min(col_map.values()) if len(col_map) > 1 else 100
        if abs(field_x - cell['x']) < x_span * 0.20:
            # Keep the closest match for each field
            if field not in cell_map or abs(field_x - cell['x']) < abs(col_map.get(field, 0) - cell_map.get(field + '_x', 0)):
                cell_map[field] = cell['text']
                cell_map[field + '_x'] = cell['x']

    if not cell_map:
        # Fallback: use first cell as bar mark
        cell_map['bar_mark'] = row[0]['text']

    bar_mark = cell_map.get('bar_mark', '').strip().lower()
    if not bar_mark:
        return None

    count_val = _parse_count(cell_map.get('count', ''))

    return {
        'bar_mark':       bar_mark,
        'component':      None,   # filled in by caller
        'reinforcement_text': None,
        'bar_dia_mm':     _parse_dia(cell_map.get('bar_dia_mm', '')),
        'spacing_mm':     _parse_spacing(cell_map.get('spacing_mm', '')),
        # count_text cleared when parse fails — prevents _norm_count() in comparator from
        # reading trailing digits in multiplier notation like 'x4' (4 piles) as a bar count.
        'count_text':     str(count_val) if count_val is not None else '',
        'count':          count_val,
        'length_m':       _safe_float(cell_map.get('length_m')),
        'total_length_m': _safe_float(cell_map.get('total_length_m')),
        'unit_wt_kg_m':   _safe_float(cell_map.get('unit_wt_kg_m')),
        'total_wt_kg':    _safe_float(cell_map.get('total_wt_kg')),
        'shape_dimensions': None,
        'from_dxf':       True,   # signals comparator to skip checks unavailable from DXF
    }


# ── Title block extraction ────────────────────────────────────────────────────

def _extract_title_block(doc, msp, all_text: list, extents: tuple) -> dict:
    """Extract title block fields. Tries ATTRIB entities first, then text patterns."""
    result = {}

    # Pass 1: INSERT blocks with ATTRIB entities (standard CASAD template)
    try:
        for insert in msp.query('INSERT'):
            for attrib in insert.attribs:
                tag = (attrib.dxf.tag or '').upper().strip()
                val = (attrib.dxf.text or '').strip()
                if not val:
                    continue
                # Map common AutoCAD title block ATTRIB tags
                if tag in ('DWG_NO', 'DRG_NO', 'DRAWING_NO', 'DRAWING_NUMBER', 'DWG_NUMBER'):
                    result['drawing_number'] = val
                elif tag in ('REV', 'REVISION', 'REV_NO'):
                    result['revision'] = val
                elif tag in ('TITLE', 'DWG_TITLE', 'DRAWING_TITLE'):
                    result['title'] = val
                elif tag in ('DATE', 'DRAWN_DATE', 'DRG_DATE'):
                    result['date'] = val
                elif tag in ('DRAWN', 'DRAWN_BY', 'DRAWNBY'):
                    result['drawn_by'] = val
                elif tag in ('DESIGN', 'DESIGN_BY', 'DESIGNED', 'DESIGNEDBY'):
                    result['design_by'] = val
                elif tag in ('APPROVED', 'APPROVED_BY', 'APPROVEDBY', 'CHECKED_BY'):
                    result['approved_by'] = val
                elif tag in ('SCALE', 'DRAWING_SCALE'):
                    result['scale'] = val
                elif tag in ('PROJECT', 'PROJECT_NAME', 'PROJ_NAME'):
                    result['project_name'] = val
                elif tag in ('SPANS', 'SPAN'):
                    result['spans'] = val
    except Exception as e:
        log.debug('ATTRIB scan failed: %s', e)

    # Pass 2: Text pattern matching in bottom-right quadrant of the drawing
    x_min, y_min, x_max, y_max = extents
    # Title block is in bottom-right ~35% horizontally and bottom 25% vertically
    tb_x_min = x_min + (x_max - x_min) * 0.60
    tb_y_max = y_min + (y_max - y_min) * 0.30

    tb_text = [t for t in all_text if t['x'] >= tb_x_min and t['y'] <= tb_y_max]

    for t in tb_text:
        s = t['text'].strip()
        su = s.upper()

        if not result.get('revision') and re.match(r'^R\d+$', s, re.IGNORECASE):
            result['revision'] = s.upper()

        if not result.get('date') and re.match(r'\d{2}[/-]\d{2}[/-]\d{4}', s):
            result['date'] = s

        if not result.get('scale') and re.search(r'AS SHOWN|NTS|\b1\s*:\s*\d+\b', su):
            result['scale'] = s

        if not result.get('drawing_number') and re.search(r'[A-Z]{2,}/[A-Z]{2,}/', s):
            result['drawing_number'] = re.sub(r'\s+', '', s)

        # Spans: "30.0M - 30.0M" pattern
        if not result.get('spans'):
            m = re.search(r'(\d+\.?\d*\s*M\s*[-–]\s*\d+\.?\d*\s*M)', su)
            if m:
                result['spans'] = m.group(1)

        # Width: "16.6M WIDE"
        if not result.get('width'):
            m = re.search(r'(\d+\.?\d*\s*M)\s+WIDE', su)
            if m:
                result['width'] = m.group(1)

        # Pier range: "P3 TO P7"
        if not result.get('pier_range'):
            m = re.search(r'(P\d+\s+TO\s+P\d+)', su)
            if m:
                result['pier_range'] = m.group(1)

        # Names with initials: "A.B.NAME"
        if re.match(r'^[A-Z]\.[A-Z]\.\w+$', s):
            if not result.get('drawn_by'):
                result['drawn_by'] = s
            elif not result.get('design_by'):
                result['design_by'] = s
            elif not result.get('approved_by'):
                result['approved_by'] = s

    # Drawing title: typically appears as "DETAILS OF PILE, PILECAP AND PIER" somewhere
    for t in all_text:
        if not result.get('title') and 'DETAILS OF PILE' in t['text'].upper():
            result['title'] = t['text'].strip()
            break

    result['bbox'] = None  # position data comes from pdfplumber merge
    return result


# ── Notes extraction ──────────────────────────────────────────────────────────

def _extract_notes(all_text: list, extents: tuple) -> dict:
    """Extract engineering notes: pile length, fixity, concrete grades, steel grade."""
    notes = {}

    # Find the NOTES section label
    notes_anchor = None
    for t in all_text:
        if t['text'].strip().upper() in ('NOTES', 'NOTE', 'GENERAL NOTES'):
            notes_anchor = t
            break

    # Scan all text (full drawing) or just near NOTES anchor
    x_min, y_min, x_max, y_max = extents
    if notes_anchor:
        # Notes are typically within 40% of drawing height below the label
        dy = (y_max - y_min) * 0.40
        scan = [t for t in all_text
                if abs(t['x'] - notes_anchor['x']) < (x_max - x_min) * 0.40
                and (notes_anchor['y'] - dy) <= t['y'] <= notes_anchor['y'] + 5]
    else:
        # Fall back: scan the left 55% of the drawing
        scan = [t for t in all_text if t['x'] < x_min + (x_max - x_min) * 0.55]

    full_text = ' '.join(t['text'] for t in scan)
    full_upper = full_text.upper()

    # Pile length
    m = re.search(r'PILE\s+LENGTH\s*[=:]\s*([\d.]+)\s*M?', full_upper)
    if m:
        notes['pile_length_m'] = _safe_float(m.group(1))

    # Pile fixity
    m = re.search(r'FIXITY\s*[=:]\s*([\d.]+)\s*M?|FIX\.?\s*LENGTH\s*[=:]\s*([\d.]+)', full_upper)
    if m:
        notes['pile_fixity_m'] = _safe_float(m.group(1) or m.group(2))

    # Pile dia
    m = re.search(r'PILE\s+DIA\.?\s*[=:]\s*([\d.]+)\s*M?', full_upper)
    if m:
        notes['pile_dia_m'] = _safe_float(m.group(1))

    # Concrete grades — find "M40 PILE", "M35 PILECAP" etc.
    for line in full_text.split('\n'):
        lu = line.upper()
        m = re.search(r'\b(M\d+)\b', lu)
        if m:
            grade = m.group(1)
            if 'PILE' in lu and 'PILECAP' not in lu and 'concrete_pile' not in notes:
                notes['concrete_pile'] = grade
            elif 'PILECAP' in lu and 'concrete_pilecap' not in notes:
                notes['concrete_pilecap'] = grade
            elif 'PIER' in lu and 'concrete_pier' not in notes:
                notes['concrete_pier'] = grade
            else:
                # Generic grade — apply to any unset component
                for comp_key in ('concrete_pile', 'concrete_pilecap', 'concrete_pier'):
                    notes.setdefault(comp_key, grade)

    # Steel grade
    m = re.search(r'\b(Fe\s*\d+|FE\s*\d+|HYSD|TMT)\b', full_upper)
    if m:
        notes['steel_grade'] = re.sub(r'\s+', '', m.group(1))

    # Lap length concrete grade
    m = re.search(r'LAP\s+LENGTH\s+FOR\s+BAR\s+FOR\s+(M\d+)', full_upper)
    if m:
        notes['lap_length_concrete_grade'] = m.group(1)

    # Max pile load
    m = re.search(r'MAX\.?\s+(?:SAFE\s+)?PILE\s+LOAD\s*[=:]\s*([\d.]+)\s*T', full_upper)
    if m:
        notes['max_pile_load_t'] = _safe_float(m.group(1))

    notes['bbox'] = None
    return notes


# ── DIMENSION entity extraction ───────────────────────────────────────────────

def _strip_dim_override(text: str) -> str:
    """Strip AutoCAD MTEXT formatting codes from a DIMENSION override string."""
    if not text:
        return ''
    text = re.sub(r'\{\\[^}]+\}', '', text)     # {\\W1;...} style blocks
    text = re.sub(r'\\[Xx]', ' ', text)          # \X stacking separator
    text = re.sub(r'\\[Pp]', ' ', text)          # \P paragraph break
    text = re.sub(r'\\[A-Za-z][^;\\]*?;', '', text)  # \code; inline codes
    text = text.replace('<>', '').strip()         # <> = "show measured value"
    return re.sub(r'\s{2,}', ' ', text).strip()


def _extract_dimensions(msp, extents: tuple) -> dict:
    """
    Extract DIMENSION entities from modelspace and classify them.

    CASAD PPP drawings use DIMENSION entities to annotate:
      - Cross-section bar counts: override text "NN -NN NOS" or "NN-NN NOS"
      - Pile/pier diameter callouts: override "NNNN DIA"
      - Pile confinement zone lengths: override "NNNN\\Xy" (bar y covers NNNN mm)
      - Raw geometric measurements: pile length, bar spacings, member dimensions

    Returns dict:
      pile_length_mm: float or None  (largest linear dim in left drawing area)
      pile_dia_mm:    float or None  (from "NNNN DIA" override)
      bar_count_annotations: [{bar_dia_mm, count, zone_mm, x_pct, y_pct}]
        — bar counts per cross-section zone as written by drafter
      confinement_zones:     [{bar_mark, length_mm, is_remaining, x_pct, y_pct}]
        — pile confinement zone heights for bars y/y1 etc.
      geometric_dims:        [{val_mm, x_pct, y_pct}]
        — uncategorised measurements (spacings, pilecap dims, etc.)
    """
    x_min, y_min, x_max, y_max = extents
    dw = (x_max - x_min) or 1.0
    dh = (y_max - y_min) or 1.0

    result = {
        'pile_length_mm':      None,
        'pile_dia_mm':         None,
        'bar_count_annotations': [],
        'confinement_zones':   [],
        'geometric_dims':      [],
    }

    for e in msp.query('DIMENSION'):
        try:
            val = float(e.get_measurement())
            if val <= 0 or val > 1e6:
                continue

            try:
                tm = e.dxf.text_midpoint
                tx, ty = float(tm.x), float(tm.y)
            except AttributeError:
                try:
                    dp = e.dxf.defpoint
                    tx, ty = float(dp.x), float(dp.y)
                except AttributeError:
                    continue

            x_pct = round((tx - x_min) / dw * 100, 1)
            y_pct = round((y_max - ty) / dh * 100, 1)

            raw = e.dxf.get('text', '') or ''
            override = _strip_dim_override(raw)
            override_u = override.upper()

            if override:
                # Pattern 1: cross-section bar count — "NN -NN NOS" (may appear multiple times)
                matches = re.findall(r'(\d+)\s*[-–]\s*(\d+)\s*NOS', override_u)
                if matches:
                    for dia_s, cnt_s in matches:
                        result['bar_count_annotations'].append({
                            'bar_dia_mm': int(dia_s),
                            'count':      int(cnt_s),
                            'zone_mm':    round(val, 1),
                            'x_pct': x_pct, 'y_pct': y_pct,
                        })
                    continue

                # Pattern 2: diameter callout — "NNNN DIA"
                m = re.search(r'(\d{3,5}(?:\.\d+)?)\s*DIA', override_u)
                if m:
                    dia = float(m.group(1))
                    if 300 < dia < 5000:   # sanity: 300–5000mm
                        if result['pile_dia_mm'] is None:
                            result['pile_dia_mm'] = dia
                    continue

                # Pattern 3: confinement zone — "NNNN bar_mark" e.g. "3600 y", "1900 y1"
                # Also: "REMAINING bar_mark" for the tail of a multi-zone bar
                lo = override.strip().lower()
                m = re.search(r'^(\d+)\s+([a-z]\d*)\s*$', lo)
                if m:
                    result['confinement_zones'].append({
                        'bar_mark':    m.group(2),
                        'length_mm':   int(m.group(1)),
                        'is_remaining': False,
                        'x_pct': x_pct, 'y_pct': y_pct,
                    })
                    continue
                m = re.search(r'^remaining\s+([a-z]\d*)\s*$', lo)
                if m:
                    result['confinement_zones'].append({
                        'bar_mark':    m.group(1),
                        'length_mm':   0,
                        'is_remaining': True,
                        'x_pct': x_pct, 'y_pct': y_pct,
                    })
                    continue

            # Check if override explicitly names pile length before using raw value
            if override:
                ov_u = override.upper()
                if ('PILE LENGTH' in ov_u or 'LENGTH OF PILE' in ov_u) and '=' in ov_u:
                    m = re.search(r'=\s*([\d.]+)', ov_u)
                    if m:
                        pl = float(m.group(1))
                        if 1000 < pl < 50000:
                            result['pile_length_mm'] = pl
                            continue

            # Raw geometric measurement with no parseable override — store for reference.
            # Do NOT infer pile_length from raw unlabeled dimensions; too many false matches
            # (pier height, span segments, foundation depth all fall in the pile-length range).
            val_mm = round(val, 1)
            result['geometric_dims'].append({'val_mm': val_mm, 'x_pct': x_pct, 'y_pct': y_pct})

        except Exception as ex:
            log.debug('DIMENSION parse error: %s', ex)

    log.info(
        'DXF dimensions: pile_len=%s pile_dia=%s bar_counts=%d zones=%d geom=%d',
        result['pile_length_mm'], result['pile_dia_mm'],
        len(result['bar_count_annotations']),
        len(result['confinement_zones']),
        len(result['geometric_dims']),
    )
    return result


# ── TABLE-1 (pier levels) extraction ─────────────────────────────────────────

def _extract_table1(all_text: list, extents: tuple) -> list:
    """Extract TABLE-1 pier elevation data."""
    # Find TABLE-1 label
    table1_anchor = None
    for t in all_text:
        if re.search(r'TABLE[\s\-]*1', t['text'].upper()):
            table1_anchor = t
            break

    if not table1_anchor:
        return []

    x_min, y_min, x_max, y_max = extents
    dw = x_max - x_min
    dh = y_max - y_min

    # Collect text near TABLE-1: within 25% drawing width, below the label
    nearby = [t for t in all_text
              if abs(t['x'] - table1_anchor['x']) < dw * 0.30
              and (table1_anchor['y'] - dh * 0.30) <= t['y'] <= table1_anchor['y'] + 5]

    rows = _group_rows(nearby, tol_frac=0.004, extents=extents)
    if len(rows) < 2:
        return []

    # First row after label = headers, subsequent rows = pier data
    # Identify which column has pier ID (P3, P4...) vs numeric levels
    table_rows = []
    for row in rows[1:]:  # skip the TABLE-1 label row
        row_text = [t['text'].strip() for t in row]
        # Look for pier ID pattern: P3, I1, N2, A1, etc. (letter + digits)
        pier_ids = [s for s in row_text if re.match(r'^[A-Z]\d+$', s, re.IGNORECASE)]
        level_vals = [_safe_float(s) for s in row_text if _safe_float(s) is not None]

        if not pier_ids and not level_vals:
            continue  # header row or gap

        for pier_id in pier_ids:
            entry = {'pier_id': pier_id, 'bbox': None}
            # CASAD TABLE-1 columns list elevations in ascending order (lowest first):
            # col 0 = bottom of pilecap, col 1 = top of pilecap, col 2 = top of pier,
            # col 3 = top of pier cap, col 4 = ground level (if present).
            level_keys = ['bottom_pilecap_m', 'top_pilecap_m', 'top_pier_m', 'top_pier_cap_m', 'ground_level_m']
            for i, key in enumerate(level_keys):
                entry[key] = level_vals[i] if i < len(level_vals) else None
            table_rows.append(entry)

    return table_rows


# ── Section info extraction ───────────────────────────────────────────────────

def _extract_section_info(all_text: list, extents: tuple) -> tuple:
    """
    Return (section_view_positions, cut_letters).
    section_view_positions: {label_text: {x,y,w,h}} in PDF-style percentages.
    cut_letters: set of single uppercase letters appearing ≥2 times in left portion.
    """
    x_min, y_min, x_max, y_max = extents
    dw = x_max - x_min
    dh = y_max - y_min

    # Left 55% = drawing views area (schedule is in right ~45%).
    # TRIGGER_WORDS like TABLE-1, SCHEDULE can appear in the right area too —
    # scan all text for labels but only count cut letters from the left area.
    view_x_max = x_min + dw * 0.55

    all_rows = _group_rows(all_text, tol_frac=0.005, extents=extents)

    section_view_positions = {}
    single_letter_counts = {}

    for row in all_rows:
        row_sorted = sorted(row, key=lambda t: t['x'])
        line = ' '.join(t['text'] for t in row_sorted).upper().strip()
        # Normalise non-ASCII hyphens
        line = line.replace('\xad', '-').replace('–', '-').replace('—', '-')

        if any(tw in line for tw in TRIGGER_WORDS):
            x0  = min(t['x'] for t in row)
            x1  = max(t['x'] for t in row)
            y_c = row[0]['y']
            # Estimate view height as 18% of drawing height (matches pdfplumber heuristic)
            view_h = dh * 0.18
            bbox = _to_bbox(x0, y_c, x1 + dw * 0.01, y_c - view_h, extents)
            section_view_positions[line[:80]] = bbox

        # Cut letter detection only in the left (view) area to avoid picking up
        # schedule row annotations, bar marks, pier labels on the right side.
        if all(t['x'] < view_x_max for t in row):
            for t in row:
                s = t['text'].strip()
                if len(s) == 1 and s.isupper() and s.isalpha():
                    single_letter_counts[s] = single_letter_counts.get(s, 0) + 1

    # Also scan ATTDEF/ATTRIB single letters — AutoCAD cut marks are often separate entities
    for t in all_text:
        if t['x'] < view_x_max:
            s = t['text'].strip()
            if len(s) == 1 and s.isupper() and s.isalpha():
                single_letter_counts[s] = single_letter_counts.get(s, 0) + 1

    cut_letters = {letter for letter, count in single_letter_counts.items() if count >= 2}

    log.info('Section info: %d labels, cut_letters=%s', len(section_view_positions), sorted(cut_letters))
    return section_view_positions, cut_letters


# ── Cross-section bar counting ────────────────────────────────────────────────

def _count_cross_section_bars(msp, all_text: list, schedule: dict, extents: tuple) -> list:
    """
    Count CIRCLE entities within each section view to get exact bar counts.
    Returns list of cross_section_check dicts matching pdf_extractor format.
    """
    x_min, y_min, x_max, y_max = extents
    dw = x_max - x_min
    dh = y_max - y_min

    # Collect all CIRCLE entities
    circles = []
    try:
        for e in msp.query('CIRCLE'):
            c = e.dxf.center
            r = e.dxf.radius
            circles.append({'x': float(c.x), 'y': float(c.y), 'r': float(r)})
    except Exception as e:
        log.warning('CIRCLE query failed: %s', e)

    if not circles:
        log.info('No CIRCLE entities found — cross-section bar counting skipped')
        return []

    log.info('Found %d CIRCLE entities total', len(circles))

    # Find section labels in the left 55% of drawing
    view_x_max = x_min + dw * 0.55
    results = []

    for t in all_text:
        if t['x'] >= view_x_max:
            continue
        # Match "SECTION Z-Z" or "SECTION A-A FOR PILE" etc.
        m = re.search(r'SECTION\s+([A-Z])[\-\xad–](\1)', t['text'].upper())
        if not m:
            continue

        letter = m.group(1)
        label_text = t['text'].upper().strip()
        lx, ly = t['x'], t['y']

        comp = _infer_component_from_label(label_text, letter)
        bar_mark = _find_bar_mark_for_section(letter, comp, schedule)

        # Search for bar circles in a region around the section label
        # Typical section view is below and/or beside the label
        # Search in a box: ±25% dw horizontally, from +5% to -40% dh vertically from label
        search_x0 = lx - dw * 0.25
        search_x1 = lx + dw * 0.25
        search_y0 = ly - dh * 0.40   # below label (DXF y decreases downward)
        search_y1 = ly + dh * 0.05   # slightly above label

        # Bar circles have small radius (< 5% drawing height) — filters out section boundary circles
        max_bar_r = dh * 0.05
        nearby = [c for c in circles
                  if search_x0 <= c['x'] <= search_x1
                  and search_y0 <= c['y'] <= search_y1
                  and c['r'] <= max_bar_r]

        if not nearby:
            continue

        # Cluster nearby circles: group circles that are close together into one section view
        cluster = _largest_cluster(nearby, max_gap=dw * 0.15)
        if not cluster:
            continue

        bar_count = len(cluster)
        spacing_issues = _compute_spacing_issues(cluster) if bar_count >= 3 else []

        # Detect bundle bars: pairs of circles very close together
        is_bundle = _detect_bundles(cluster)

        # BBox of the cluster
        cx0 = min(c['x'] for c in cluster) - dw * 0.01
        cx1 = max(c['x'] for c in cluster) + dw * 0.01
        cy0 = min(c['y'] for c in cluster) - dh * 0.01
        cy1 = max(c['y'] for c in cluster) + dh * 0.01
        bbox = _to_bbox(cx0, cy0, cx1, cy1, extents)

        results.append({
            'section_name': f'{letter}-{letter}',
            'component':    comp or 'unknown',
            'bar_mark':     bar_mark or '',
            'visual_count': bar_count,
            'is_bundle':    is_bundle,
            'spacing_uniform': len(spacing_issues) == 0,
            'spacing_issues':  spacing_issues,
            'bbox': bbox,
        })
        log.info('Section %s-%s (%s): %d circles, bundle=%s, spacing_issues=%d',
                 letter, letter, comp, bar_count, is_bundle, len(spacing_issues))

    return results


def _largest_cluster(circles: list, max_gap: float) -> list:
    """Return the largest group of circles where each is within max_gap of another."""
    if not circles:
        return []
    # Simple greedy clustering: start from first circle, expand group
    groups = []
    remaining = list(circles)

    while remaining:
        seed = remaining.pop(0)
        group = [seed]
        changed = True
        while changed:
            changed = False
            still_remaining = []
            for c in remaining:
                if any(math.hypot(c['x'] - g['x'], c['y'] - g['y']) <= max_gap for g in group):
                    group.append(c)
                    changed = True
                else:
                    still_remaining.append(c)
            remaining = still_remaining
        groups.append(group)

    return max(groups, key=len)


def _compute_spacing_issues(cluster: list) -> list:
    """Detect angular spacing irregularities for bars arranged in a ring."""
    if len(cluster) < 4:
        return []

    cx = sum(c['x'] for c in cluster) / len(cluster)
    cy = sum(c['y'] for c in cluster) / len(cluster)

    angles = sorted(math.atan2(c['y'] - cy, c['x'] - cx) for c in cluster)
    n = len(angles)
    expected_gap = 2 * math.pi / n

    issues = []
    for i in range(n):
        a_next = angles[(i + 1) % n]
        a_curr = angles[i]
        gap = (a_next - a_curr) % (2 * math.pi)
        mid_angle = a_curr + gap / 2
        clock_pos = _angle_to_clock(mid_angle)

        if gap > expected_gap * 1.6:
            issues.append({
                'type': 'gap',
                'location': f'approx {clock_pos}',
                'description': f'Arc gap larger than expected (no bar between positions)',
            })
        elif gap < expected_gap * 0.4:
            issues.append({
                'type': 'clustering',
                'location': f'approx {clock_pos}',
                'description': f'Bars unusually close together',
            })

    return issues


def _detect_bundles(cluster: list) -> bool:
    """Return True if bars appear to be bundle bars (closely-spaced pairs)."""
    if len(cluster) < 2:
        return False
    # Compute all pairwise distances
    pairs = []
    for i, a in enumerate(cluster):
        for b in cluster[i+1:]:
            pairs.append(math.hypot(a['x'] - b['x'], a['y'] - b['y']))
    pairs.sort()
    # If the smallest distance is much less than the median, likely bundles
    median_d = pairs[len(pairs) // 2]
    return pairs[0] < median_d * 0.3 if median_d > 0 else False


def _angle_to_clock(angle_rad: float) -> str:
    """Convert angle in radians (0=right, CCW) to clockface position."""
    # Clockface: 12 o'clock = top = pi/2 radians
    clock_hour = ((-angle_rad + math.pi / 2) / (2 * math.pi) * 12) % 12
    hour = int(clock_hour) or 12
    minute = int((clock_hour % 1) * 60)
    return f"{hour}:{minute:02d} o'clock"


def _infer_component_from_label(label: str, letter: str) -> str:
    """Infer pile/pilecap/pier component from section label text."""
    u = label.upper()
    if 'PILE' in u and 'PILECAP' not in u:
        return 'pile'
    if 'PILECAP' in u:
        return 'pilecap'
    if 'PIER' in u:
        return 'pier'
    return _SECTION_LETTER_COMP.get(letter, 'unknown')


def _find_bar_mark_for_section(letter: str, comp: str, schedule: dict) -> str | None:
    """Best-guess bar mark for a given section letter and component."""
    # Conventional: Z-Z = pile longitudinal (x), A-A pile = x, A-A pilecap = a or b
    letter_to_mark = {
        'pile':    {'Z': 'x', 'A': 'x'},
        'pilecap': {'A': 'a', 'B': 'a', 'C': 'a'},
        'pier':    {'C': 'g', 'D': 'g'},
    }
    if comp in letter_to_mark:
        mark = letter_to_mark[comp].get(letter)
        if mark and comp in schedule and mark in schedule[comp]:
            return mark
    # If not found by convention, return the first bar mark in the component
    if comp in schedule and schedule[comp]:
        return next(iter(schedule[comp]))
    return None


# ── Completeness checks ───────────────────────────────────────────────────────

def _check_required_sections(section_view_positions: dict) -> list:
    """Return presence status for each required PPP section, mirroring pdf_extractor logic."""
    all_labels = ' '.join(section_view_positions.keys()).upper()
    result = []
    for name, keywords in _REQUIRED_PPP_SECTIONS:
        present = any(kw.upper() in all_labels for kw in keywords)
        bbox = None
        if present:
            for label, pos in section_view_positions.items():
                if any(kw.upper() in label for kw in keywords):
                    bbox = pos
                    break
        result.append({'name': name, 'present': present, 'bbox': bbox})
    return result


def _check_notes_completeness(all_text: list) -> list:
    """Return presence status for each required note item via keyword scan of DXF text."""
    full_upper = ' '.join(t['text'] for t in all_text).upper()
    concrete_keys = ('concrete_pile', 'concrete_pilecap', 'concrete_pier')
    concrete_found = any(kw in full_upper for kw in ('M30', 'M35', 'M40', 'M45', 'M50'))
    result = []
    for item_key, keywords in _NOTE_KEYWORDS.items():
        if item_key in concrete_keys:
            present = concrete_found
        else:
            present = any(kw.upper() in full_upper for kw in keywords)
        result.append({'item': item_key, 'present': present, 'value': None})
    return result


# ── Number parsing utilities ──────────────────────────────────────────────────

def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(',', '').strip())
    except (ValueError, TypeError):
        return None


def _parse_dia(text: str) -> int | None:
    """Parse bar diameter from strings like '25Ø', '25φ', 'T25', '25'."""
    if not text:
        return None
    s = str(text).strip()
    m = re.search(r'(\d+)\s*[ØφΦø]', s)
    if m:
        return int(m.group(1))
    m = re.search(r'[Tt](\d+)', s)
    if m:
        return int(m.group(1))
    m = re.match(r'^(\d+)$', s.strip())
    if m:
        v = int(m.group(1))
        if v in _VALID_DIA:
            return v
    return None


def _parse_spacing(text: str) -> int | None:
    """Parse c/c spacing from strings like '150', '150 c/c', '-'."""
    if not text:
        return None
    s = str(text).strip()
    if s in ('-', 'N.A.', 'NA', '', 'None', 'nil', 'NIL'):
        return None
    m = re.search(r'(\d+(?:\.\d+)?)', s)
    return int(float(m.group(1))) if m else None


def _parse_count(text: str) -> int | None:
    """Parse count expression: '42', '4×13=52', '21*4=84'."""
    if not text:
        return None
    s = str(text).strip()
    if not s or s in ('-', 'N.A.', 'NA'):
        return None
    # If expression contains =, use the right-hand side (the total)
    if '=' in s:
        s = s.split('=')[-1].strip()
    try:
        return int(float(s.replace(',', '')))
    except (ValueError, TypeError):
        pass
    # Try evaluating multiplication expressions: 4×13, 21*4
    try:
        expr = re.sub(r'[×xX×]', '*', s)
        expr = re.sub(r'[^\d*.+\-]', '', expr)
        if expr and re.match(r'^[\d*+\-]+$', expr):
            return int(eval(expr))  # noqa: S307 — safe: only digits and operators
    except Exception:
        pass
    return None
