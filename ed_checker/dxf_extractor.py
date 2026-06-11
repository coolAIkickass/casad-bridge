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
import re
import math
import logging

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

# Component section header keywords that appear in the schedule
_COMP_KEYWORDS = {
    'pilecap': ('PILECAP',),
    'pile':    ('PILE (SCHEDULE', 'PILE (PER PILE', 'PILE (PILE'),
    'pier':    ('PIER (CIRCULAR', 'PIER (RECTANGULAR', 'PIER (CAPSULE', 'PIER (REC'),
}

# Schedule column header keywords → internal field name
_COL_KEYWORDS = {
    'bar_mark':       ['BAR MARK', 'BAR\nMARK', 'MARK'],
    'bar_dia_mm':     ['DIA', 'Ø', 'φ', 'PHI', 'DIAMETER'],
    'spacing_mm':     ['SPACING', 'C/C', 'PITCH', 'SPC'],
    'count':          ['NOS', 'NO.', 'NUMBER', 'COUNT', 'NOS.'],
    'length_m':       ['LENGTH OF BAR', 'LENGTH OF', 'BAR LENGTH', 'LENGTH (M)', 'LENGTH(M)', 'LENGTH'],
    'total_length_m': ['TOTAL LENGTH', 'TOTAL LEN', 'TOT. LEN'],
    'unit_wt_kg_m':   ['UNIT WT', 'UNIT WEIGHT', 'WT./M', 'KG/M', 'UNIT W'],
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

    try:
        doc = ezdxf.read(io.BytesIO(dxf_bytes))
    except Exception as e:
        log.error('DXF parse failed: %s', e, exc_info=True)
        return _empty()

    msp     = doc.modelspace()
    extents = _get_extents(doc)
    all_text = _collect_text(msp)

    log.info('DXF: %d text entities, extents=(%.1f,%.1f)–(%.1f,%.1f)',
             len(all_text), *extents)

    schedule            = _extract_schedule(msp, all_text, extents)
    title_block         = _extract_title_block(doc, msp, all_text, extents)
    notes               = _extract_notes(all_text, extents)
    table_1             = _extract_table1(all_text, extents)
    sv_pos, cut_letters = _extract_section_info(all_text, extents)
    xsec                = _count_cross_section_bars(msp, all_text, schedule, extents)

    log.info('DXF extraction done: comps=%s title_fields=%d sections=%d cuts=%s xsec=%d',
             list(schedule.keys()),
             sum(1 for v in title_block.values() if v),
             len(sv_pos), sorted(cut_letters), len(xsec))

    return {
        'schedule':                     schedule,
        'title_block':                  title_block,
        'notes':                        notes,
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
            text = (e.dxf.text or '').strip()
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

def _extract_schedule(msp, all_text: list, extents: tuple) -> dict:
    """Extract reinforcement schedule from DXF text entities."""
    x_min, y_min, x_max, y_max = extents
    dw = x_max - x_min

    # Schedule occupies the right ~45% of the drawing
    sched_x_min = x_min + dw * 0.50
    sched_text = [t for t in all_text if t['x'] >= sched_x_min]

    if not sched_text:
        log.warning('No text found in schedule area (x >= %.1f)', sched_x_min)
        return {}

    log.info('Schedule area: %d text entities at x >= %.1f', len(sched_text), sched_x_min)

    # Group into rows
    rows = _group_rows(sched_text, tol_frac=0.004, extents=extents)
    if not rows:
        return {}

    # Find component headers and column headers
    comp_rows   = {}   # comp_name → row index in `rows`
    header_idx  = None

    for idx, row in enumerate(rows):
        row_text = ' '.join(t['text'] for t in row).upper().strip()
        # Normalise typography
        row_text = row_text.replace('\xad', '-').replace('–', '-')

        # Column header row detection
        if header_idx is None and any(k in row_text for k in ('DIA', ' NOS', 'LENGTH', 'UNIT WT')):
            header_idx = idx
            continue

        # Component header detection
        for comp, keywords in _COMP_KEYWORDS.items():
            if any(row_text.startswith(kw) for kw in keywords) or any(kw == row_text for kw in keywords):
                comp_rows[comp] = idx

    if header_idx is None:
        # Try looser match: any row with BAR anywhere
        for idx, row in enumerate(rows):
            if 'BAR' in ' '.join(t['text'] for t in row).upper():
                header_idx = idx
                break

    if header_idx is None:
        log.warning('Schedule column header row not found')
        return {}

    col_map = _build_col_map(rows[header_idx])
    log.info('Schedule column map: %s', {k: f'x≈{v:.1f}' for k, v in col_map.items()})

    # Determine component assignment for each row by proximity to component header
    # Build sorted list: (row_index, comp_name) for component headers
    comp_boundaries = sorted(comp_rows.items(), key=lambda kv: kv[1])

    def _row_component(row_idx):
        comp = None
        for name, cidx in comp_boundaries:
            if row_idx > cidx:
                comp = name
        return comp

    # Parse data rows
    schedule = {}
    for idx, row in enumerate(rows[header_idx + 1:], start=header_idx + 1):
        # Skip if this is a component header row
        if idx in {v for v in comp_rows.values()}:
            continue

        comp = _row_component(idx)
        if comp is None:
            continue

        bar_data = _parse_schedule_row(row, col_map)
        if not bar_data:
            continue
        bm = bar_data.get('bar_mark', '').strip().lower()
        if not bm or bm not in _KNOWN_MARKS:
            continue

        comp_dict = schedule.setdefault(comp, {})
        if bm in comp_dict:
            # Duplicate bar mark — accumulate totals (e.g. two y rows for pile)
            ex = comp_dict[bm]
            if not isinstance(ex, dict):
                continue
            for f in ('count', 'total_length_m', 'total_wt_kg'):
                ov = _safe_float(ex.get(f))
                nv = _safe_float(bar_data.get(f))
                if ov is not None and nv is not None:
                    ex[f] = ov + nv
        else:
            comp_dict[bm] = bar_data

    log.info('Schedule parsed: %s',
             {c: list(b.keys()) for c, b in schedule.items()})
    return schedule


def _build_col_map(header_row: list) -> dict:
    """Map column field name → x-center position from the header row."""
    col_map = {}
    # First pass: try to match full header phrases across adjacent cells
    full_text = ' '.join(t['text'].upper() for t in header_row)
    used_idxs = set()

    for field, keywords in _COL_KEYWORDS.items():
        for kw in sorted(keywords, key=len, reverse=True):  # longest match first
            if kw.upper() in full_text:
                # Find which cell(s) contain this keyword
                for i, t in enumerate(header_row):
                    if i in used_idxs:
                        continue
                    cell_text = t['text'].upper()
                    if kw.upper() in cell_text or cell_text in kw.upper():
                        col_map[field] = t['x']
                        used_idxs.add(i)
                        break
                if field in col_map:
                    break

    return col_map


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

    return {
        'bar_mark':       bar_mark,
        'component':      None,   # filled in by caller
        'reinforcement_text': None,
        'bar_dia_mm':     _parse_dia(cell_map.get('bar_dia_mm', '')),
        'spacing_mm':     _parse_spacing(cell_map.get('spacing_mm', '')),
        'count_text':     cell_map.get('count', ''),
        'count':          _parse_count(cell_map.get('count', '')),
        'length_m':       _safe_float(cell_map.get('length_m')),
        'total_length_m': _safe_float(cell_map.get('total_length_m')),
        'unit_wt_kg_m':   _safe_float(cell_map.get('unit_wt_kg_m')),
        'total_wt_kg':    _safe_float(cell_map.get('total_wt_kg')),
        'shape_dimensions': None,
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
        # Look for pier ID pattern
        pier_ids = [s for s in row_text if re.match(r'^P\d+$', s, re.IGNORECASE)]
        level_vals = [_safe_float(s) for s in row_text if _safe_float(s) is not None]

        if not pier_ids and not level_vals:
            continue  # header row or gap

        for pier_id in pier_ids:
            entry = {'pier_id': pier_id, 'bbox': None}
            # Assign level values by position: top_pier_cap, top_pier, top_pilecap, bottom_pilecap, ground_level
            level_keys = ['top_pier_cap_m', 'top_pier_m', 'top_pilecap_m', 'bottom_pilecap_m', 'ground_level_m']
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

    # Left 55% = drawing views area (schedule is in right ~45%)
    view_x_max = x_min + dw * 0.55

    # Group text on the left side into lines
    left_text = [t for t in all_text if t['x'] < view_x_max]
    rows = _group_rows(left_text, tol_frac=0.005, extents=extents)

    section_view_positions = {}
    single_letter_counts = {}

    for row in rows:
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

        # Track isolated single uppercase letters for cut-mark detection
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
