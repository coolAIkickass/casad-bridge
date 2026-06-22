"""
DXF-based drawing data extractor for CASAD ED Checker.
Reads an AutoCAD DXF file (exported via File > Save As > AutoCAD DXF) and produces the
same drawing_data dict as pdf_extractor.extract_from_drawing(), giving exact text values
with no OCR or PDF encoding errors.

Called from __init__.run_check() when the engineer uploads a DXF alongside the PDF.
The PDF is still required for display in the review UI; the DXF is used only for data
extraction.
"""
import gc
import io
import os
import re
import math
import logging
import tempfile

from .profiles import PPP_PROFILE, DrawingTypeProfile, TRIGGER_WORDS
from .schema import new_drawing_data, diag

log = logging.getLogger(__name__)

# Suppress ezdxf's "copy process ignored ACDB_BLOCKREPRESENTATION_DATA" warning.
# virtual_entities() copies block entities into WCS and silently skips AutoCAD-only
# extension-dictionary entries it doesn't know about. This is harmless — our text
# extraction is unaffected — but the warning fires once per INSERT referencing the block.
class _EzdxfCopyIgnoreFilter(logging.Filter):
    def filter(self, record):
        return 'copy process ignored' not in record.getMessage()

logging.getLogger('ezdxf').addFilter(_EzdxfCopyIgnoreFilter())

# DXF $INSUNITS code → millimetres per drawing unit.
# Codes: 1=inches, 2=feet, 4=mm, 5=cm, 6=m, 7=km. 0=unitless (assume mm — CASAD standard).
_INSUNITS_TO_MM = {1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0, 7: 1e6}

# Block-reference text traversal limits (B1): recursion depth and total text entity cap.
_MAX_BLOCK_DEPTH = 3
_MAX_TEXT_ENTITIES = 20000

# Schedule column header keywords → internal field name.
# CASAD schedule column headers span multiple Y-rows (e.g. "TOTAL" on one line,
# "LENGTH IN" on next, "METER" on next). _build_col_map receives all sub-rows
# and concatenates text per X-band before matching.
_COL_KEYWORDS = {
    'bar_mark':       ['BAR MARK', 'BAR\nMARK', 'MARK', 'MK.', 'MK'],
    'reinforcement':  ['REINFORCEMENT', 'REINF.', 'REINF', 'TYPE OF REINF'],
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

# Words that look like bar marks but are definitely not (column header / schedule keyword tokens).
_BAR_MARK_SKIP = {
    'dia', 'nos', 'no', 'mk', 'wt', 'kg', 'm', 'mm', 'cm',
    'pile', 'pier', 'pilecap', 'bar', 'mark', 'total', 'unit',
    'length', 'weight', 'spacing', 'count', 'number', 'shape',
}


def _is_bar_mark_token(text: str) -> bool:
    """
    True if text looks like a bar mark label: single letter + optional 1-2 digits.
    Matches: a, b, x, y1, f1, h2, i1, l, m, n, z99 — any project's convention.
    Does NOT require the mark to be in a hardcoded list.
    """
    t = text.strip().strip("'\"").lower()
    return bool(re.match(r'^[a-z]\d{0,2}$', t)) and t not in _BAR_MARK_SKIP


def _build_comp_boundaries(data_rows: list, profile: DrawingTypeProfile) -> list:
    """
    Scan data rows for component section header rows (e.g. PILECAP / PILE / PIER).
    Returns [(row_idx, comp), ...] sorted by row_idx.

    A true component header looks like:
        "PILECAP"  /  "PILE (SCHEDULE PER PILE)"  /  "PIER (CIRCULAR)"
    False positives that must be excluded:
        "PILE = 11460 KG ..."  — total weight summary row (COMP followed by = and digits)
        "MAX. LOAD ON TOP OF PILE"  — note row (PILE buried mid-sentence)
    Both are excluded by two guards:
      1. Skip any row where COMP is followed by '=' and a digit.
      2. Require the component keyword to appear in the first two tokens of the row.

    Falls back to profile.bar_mark_comp_fallback when no boundaries are found
    (drawings without explicit sub-headers rely on bar mark letter conventions).
    """
    _total_re = profile.total_row_guard_re()
    boundaries = []
    for idx, row in enumerate(data_rows):
        row_text = ' '.join(t['text'] for t in row)
        # Guard 1: skip total weight rows like "PILE = 11460 KG"
        if _total_re.search(row_text):
            continue
        tokens = row_text.split()
        first_two = ' '.join(tokens[:2]).upper()
        for comp, pattern in profile.comp_header_patterns:
            if not pattern.search(row_text):
                continue
            # Guard 2: component keyword must be within the first two tokens
            # (excludes "MAX. LOAD ON TOP OF PILE", "LOAD ON PILE CAP" etc.)
            if not pattern.search(first_two):
                continue
            boundaries.append((idx, comp))
            log.debug('Component boundary at row %d: %r → %s', idx, row_text[:60], comp)
            break
    return boundaries


def _comp_for_row(row_idx: int, boundaries: list) -> str | None:
    """
    Return the component for a given data row index using the nearest preceding boundary.
    Returns None if row_idx precedes all boundaries (no header found yet).
    """
    comp = None
    for boundary_idx, boundary_comp in boundaries:
        if boundary_idx <= row_idx:
            comp = boundary_comp
        else:
            break
    return comp


# ── Public entry point ────────────────────────────────────────────────────────

def extract_from_dxf(dxf_bytes: bytes, profile: DrawingTypeProfile = PPP_PROFILE) -> dict:
    """
    Parse an AutoCAD DXF file and return drawing_data compatible with comparator.compare().
    Produces exact values — no OCR, no vision API required.

    Every way extraction can degrade is recorded in drawing_data['extraction_diagnostics']
    ({code, message, severity}) — 'error' diagnostics become review issues so the engineer
    knows what could NOT be checked; 'info' diagnostics are visible via the debug route.
    """
    diags: list = []

    try:
        import ezdxf
    except ImportError:
        log.error('ezdxf not installed. Run: pip install ezdxf>=1.3.0')
        diags.append(diag('ezdxf_missing',
                          'The DXF could not be processed: ezdxf is not installed on the server. '
                          'All DXF-based checks were skipped.'))
        return new_drawing_data(extraction_diagnostics=diags)

    # ezdxf.read(BytesIO) fails on AutoCAD DXF files that contain binary data
    # (embedded images, binary chunks in MTEXT). ezdxf.readfile() handles encoding
    # detection correctly. Write to a temp file and use readfile() instead.
    tmp_path = None
    doc = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
            tmp.write(dxf_bytes)
            tmp_path = tmp.name
        try:
            doc = ezdxf.readfile(tmp_path)
        except Exception as strict_err:
            # Real-world DXFs are frequently malformed in recoverable ways —
            # try the recover module before giving up.
            from ezdxf import recover
            doc, auditor = recover.readfile(tmp_path)
            log.warning('DXF strict parse failed (%s) — recovered with ezdxf.recover '
                        '(%d errors audited)', strict_err, len(auditor.errors))
            diags.append(diag(
                'dxf_recovered',
                f'The DXF file was malformed and was repaired automatically '
                f'({len(auditor.errors)} structural errors fixed). Extracted values are '
                f'usually still exact, but verify results if anything looks off.',
                severity='info'))
    except Exception as e:
        log.error('DXF parse failed: %s', e, exc_info=True)
        diags.append(diag('dxf_parse_failed',
                          f'The DXF file could not be parsed ({e}). All DXF-based checks '
                          f'were skipped. Re-export via File > Save As > AutoCAD DXF.'))
        return new_drawing_data(extraction_diagnostics=diags)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    msp     = doc.modelspace()
    u2mm    = _units_to_mm(doc, diags)
    all_text, text_stats = _collect_text(msp)
    ps_text = _collect_paperspace_text(doc)
    extents = _get_extents(doc, all_text, diags)

    if not all_text and not ps_text:
        diags.append(diag('no_text_entities',
                          'The DXF contains no readable text entities — text may have been '
                          'exploded to outlines on export. All text-based DXF checks were '
                          'skipped. Re-export with text preserved (File > Save As > AutoCAD DXF).'))
    if text_stats['in_blocks']:
        diags.append(diag('block_text_found',
                          f"{text_stats['in_blocks']} text entities were read from inside "
                          f"block references.", severity='info'))
    if text_stats['errors']:
        diags.append(diag('text_entity_errors',
                          f"{text_stats['errors']} text entities could not be read and were "
                          f"skipped.", severity='info'))
    if ps_text:
        diags.append(diag('paperspace_text_found',
                          f'{len(ps_text)} text entities found in paperspace layouts '
                          f'(used for title block extraction).', severity='info'))

    log.info('DXF: %d text entities (%d from blocks, %d paperspace), units→mm=%.3f, '
             'extents=(%.1f,%.1f)–(%.1f,%.1f)',
             len(all_text), text_stats['in_blocks'], len(ps_text), u2mm, *extents)

    schedule, sched_info = _extract_schedule(msp, all_text, extents, profile, u2mm, diags)
    title_block         = _extract_title_block(doc, msp, all_text, extents, profile, ps_text)
    notes               = _extract_notes(all_text, extents, profile)
    _append_section_grade_diagnostics(all_text, extents, profile, notes, diags)
    dim_data            = _extract_dimensions(msp, extents, u2mm)
    table_1             = _extract_table1(all_text, extents, profile.layout)
    sv_pos, cut_letters = _extract_section_info(all_text, extents, profile.layout)
    xsec                = _count_cross_section_bars(msp, all_text, schedule, extents, profile, diags, u2mm)
    geometry_from_drawing = _classify_all_dims(msp, all_text, extents, profile, u2mm)
    multileader_callouts  = _extract_multileader_callouts(msp, extents)
    # Programmatic unlabeled-view and missing-detail detection (runs before del msp).
    unlabeled_circles   = _detect_unlabeled_section_circles(msp, all_text, extents, sv_pos, profile, u2mm)
    missing_detail_refs = _detect_missing_detail_refs(all_text, sv_pos)

    # ezdxf doc and modelspace are done — all data now lives in plain Python dicts.
    # Delete explicitly and gc to break cyclic entity refs (~100-200 MB) before returning.
    del doc, msp, ps_text
    gc.collect()

    # Supplement notes with DIMENSION-derived values when text extraction missed them.
    # DIMENSION entities give exact geometric measurements with no OCR error.
    if dim_data.get('pile_length_mm') and notes.get('pile_length_m') is None:
        notes['pile_length_m'] = round(dim_data['pile_length_mm'] / 1000.0, 3)
        log.info('Notes: pile_length_m set from DIMENSION entity: %.3fm', notes['pile_length_m'])
    if dim_data.get('pile_dia_mm') and notes.get('pile_dia_m') is None:
        notes['pile_dia_m'] = round(dim_data['pile_dia_mm'] / 1000.0, 3)
        log.info('Notes: pile_dia_m set from DIMENSION entity: %.3fm', notes['pile_dia_m'])

    # What this extraction can vouch for (consumed by the comparator instead of
    # source flags). Spacing is only checkable if the schedule has a C/C column.
    capabilities = {
        'spacing':          sched_info.get('has_spacing_col',    False),
        'shape_dims':       sched_info.get('has_shape_dims_col', False),
        'visual_bar_count': True,
        'label_review':     False,   # vision-only check, not run in DXF path
        'dimension_review': False,   # vision-only check, not run in DXF path
    }

    log.info('DXF extraction done: comps=%s title_fields=%d sections=%d cuts=%s xsec=%d diags=%d',
             list(schedule.keys()),
             sum(1 for v in title_block.values() if v),
             len(sv_pos), sorted(cut_letters), len(xsec), len(diags))

    return new_drawing_data(
        schedule=schedule,
        title_block=title_block,
        notes=notes,
        dim_data=dim_data,
        table_1=table_1,
        cross_section_checks=xsec,
        section_view_positions=sv_pos,
        cut_letters=cut_letters,
        # schedule_section_positions filled by pdfplumber merge in __init__.py.
        # dxf_comp_anchors: DXF-extent-% y of each component header — paired with
        # schedule_section_positions to calibrate row_bbox y into PDF-page-%.
        dxf_comp_anchors=sched_info.get('dxf_comp_anchors', {}),
        sections_from_text=_check_required_sections(sv_pos, profile),
        notes_completeness_from_text=_check_notes_completeness(all_text, profile),
        capabilities=capabilities,
        extraction_diagnostics=diags,
        raw_text=[t['text'] for t in all_text],
        geometry_from_drawing=geometry_from_drawing,
        multileader_callouts=multileader_callouts,
        # DXF-derived unlabeled section circles — merged with vision results in __init__.py
        unlabeled_views=unlabeled_circles,
        # DXF-derived missing DETAIL refs — __init__.py appends cut-letter missing sections
        missing_referenced_sections=missing_detail_refs,
    )


# ── Geometry helpers ──────────────────────────────────────────────────────────

def _get_extents(doc, all_text: list = None, diags: list = None) -> tuple:
    """
    Return (x_min, y_min, x_max, y_max) for the drawing.

    $EXTMIN/$EXTMAX from the header are used when sane, but they cannot be trusted
    blindly: files saved without a recompute carry sentinel values (+1e20/-1e20,
    inverted), and stale extents are common in real AutoCAD exports. In those cases
    the bounding box is computed from the text-entity cloud instead — wrong extents
    silently break every region filter and marker bbox downstream.
    """
    ext = None
    try:
        mn = doc.header.get('$EXTMIN', (0, 0, 0))
        mx = doc.header.get('$EXTMAX', (1000, 700, 0))
        ext = (float(mn[0]), float(mn[1]), float(mx[0]), float(mx[1]))
    except Exception:
        pass

    if ext and ext[0] < ext[2] and ext[1] < ext[3] and all(abs(v) < 1e15 for v in ext):
        return ext

    if all_text:
        xs = [t['x'] for t in all_text]
        ys = [t['y'] for t in all_text]
        mx_x, mn_x = max(xs), min(xs)
        mx_y, mn_y = max(ys), min(ys)
        pad_x = (mx_x - mn_x) * 0.02 or 10.0
        pad_y = (mx_y - mn_y) * 0.02 or 10.0
        computed = (mn_x - pad_x, mn_y - pad_y, mx_x + pad_x, mx_y + pad_y)
        log.warning('Header extents invalid (%s) — computed from text cloud: %s', ext, computed)
        if diags is not None:
            diags.append(diag('extents_computed',
                              'The DXF header drawing extents were missing or invalid; '
                              'the sheet area was derived from text positions instead. '
                              'Region-based extraction should still work, but verify results.',
                              severity='info'))
        return computed

    return 0.0, 0.0, 1000.0, 700.0


def _units_to_mm(doc, diags: list) -> float:
    """
    Return the factor that converts drawing units to millimetres, from $INSUNITS.
    All absolute-distance thresholds in this module are defined in mm and must be
    divided by this factor before comparison against raw coordinates (or measured
    values multiplied by it). Unitless/unknown drawings are assumed to be mm
    (the CASAD standard) with an info diagnostic so the assumption is visible.
    """
    try:
        code = int(doc.header.get('$INSUNITS', 0))
    except Exception:
        code = 0
    factor = _INSUNITS_TO_MM.get(code)
    if factor is None:
        if code != 0:
            diags.append(diag('units_unsupported',
                              f'DXF declares unit code {code}, which is not supported — '
                              f'assuming millimetres. Distance-based checks may be wrong '
                              f'if the drawing is not in mm.', severity='info'))
        else:
            diags.append(diag('units_assumed_mm',
                              'DXF does not declare drawing units ($INSUNITS) — assuming '
                              'millimetres (CASAD standard).', severity='info'))
        return 1.0
    if factor != 1.0:
        log.info('DXF units: $INSUNITS=%d → %.3f mm per drawing unit', code, factor)
    return factor


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

def _text_entity_to_dict(e) -> dict | None:
    """Convert one TEXT/MTEXT entity to {text, x, y}, or None if empty/unreadable."""
    etype = e.dxftype()
    if etype == 'TEXT':
        text = _strip_text_codes((e.dxf.text or '').strip())
    elif etype == 'MTEXT':
        try:
            text = e.plain_mtext().strip()
        except AttributeError:
            text = _strip_mtext_codes(e.dxf.text or '')
    elif etype == 'ATTRIB':
        text = _strip_text_codes((e.dxf.text or '').strip())
    else:
        return None
    if not text:
        return None
    p = e.dxf.insert
    return {'text': text, 'x': float(p.x), 'y': float(p.y)}


def _collect_layout_text(layout, stats: dict) -> list:
    """
    Collect [{text, x, y}] from one layout (modelspace or a paperspace layout):
    top-level TEXT/MTEXT entities plus text nested inside block references.

    Block traversal (B1): INSERT entities are flattened via virtual_entities(),
    which yields block content already transformed to layout coordinates. Nested
    INSERTs are recursed into up to _MAX_BLOCK_DEPTH. Title blocks, standard-notes
    blocks, and schedule cells inserted as blocks are invisible without this.
    """
    result = []
    for e in layout.query('TEXT MTEXT'):
        try:
            item = _text_entity_to_dict(e)
            if item:
                result.append(item)
        except Exception:
            stats['errors'] += 1
    for ins in layout.query('INSERT'):
        _collect_insert_text(ins, result, depth=0, stats=stats)
        # ATTRIBs are attached to the INSERT itself, not the block definition
        try:
            for attrib in ins.attribs:
                item = _text_entity_to_dict(attrib)
                if item:
                    item['from_block'] = True
                    item['is_attrib'] = True   # user-typed tag value; safe for cut-letter detection
                    result.append(item)
                    stats['in_blocks'] += 1
        except Exception:
            stats['errors'] += 1
    return result


def _collect_insert_text(insert, out: list, depth: int, stats: dict):
    """
    Recursively collect TEXT/MTEXT from inside a block reference (WCS coordinates).
    Items are tagged 'from_block': True — block text is used for labels, title block
    and notes, but excluded where author-typed top-level text is authoritative
    (schedule cells, cut-letter detection), because symbol blocks render glyphs
    (e.g. the 'O' diameter symbol) and annotation fragments that corrupt those passes.
    """
    if depth > _MAX_BLOCK_DEPTH or len(out) > _MAX_TEXT_ENTITIES:
        return
    try:
        for ve in insert.virtual_entities():
            etype = ve.dxftype()
            if etype in ('TEXT', 'MTEXT'):
                try:
                    item = _text_entity_to_dict(ve)
                    if item:
                        item['from_block'] = True
                        out.append(item)
                        stats['in_blocks'] += 1
                except Exception:
                    stats['errors'] += 1
            elif etype == 'INSERT':
                _collect_insert_text(ve, out, depth + 1, stats)
    except Exception:
        stats['errors'] += 1


def _collect_text(msp) -> tuple:
    """
    Return ([{text, x, y}], stats) for all text in model space — top-level TEXT/MTEXT
    plus text inside block references. stats = {'in_blocks': N, 'errors': N}.
    """
    stats = {'in_blocks': 0, 'errors': 0}
    result = _collect_layout_text(msp, stats)
    return result, stats


def _collect_paperspace_text(doc) -> list:
    """
    Collect text from all paperspace layouts (title blocks often live there).
    Coordinates are in each layout's paper space — usable for pattern matching
    and per-layout quadrant filtering, NOT comparable to modelspace extents.
    """
    stats = {'in_blocks': 0, 'errors': 0}
    result = []
    try:
        for name in doc.layout_names():
            if name.lower() == 'model':
                continue
            try:
                result.extend(_collect_layout_text(doc.layout(name), stats))
            except Exception as e:
                log.debug('Paperspace layout %r scan failed: %s', name, e)
    except Exception as e:
        log.debug('Paperspace scan failed: %s', e)
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
    """Remove AutoCAD MTEXT inline formatting codes.

    {\\Wn;text} braces are a formatting *scope*, not a content wrapper — e.g. a
    bar-count callout written as {\\W1;32 -15 NOS} must yield "32 -15 NOS", not ''.
    Strip the \\code; prefix, then strip the brace delimiters themselves, but never
    delete the text they enclose.
    """
    text = re.sub(r'\\[PpLlOoKkCcFfAaQqHhWwBbIiTtSs][^;]*;', '', text)
    text = text.replace('{', '').replace('}', '')
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


def _aggregate_bar_rows(bar_rows: list, col_map: dict, bm: str,
                        x_offset_min: float = 1000.0) -> dict | None:
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
    if abs(x_offset) > x_offset_min:
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

    # Reinforcement column text (e.g. "16φ@150 c/c" or "25φ – 42 NOS.")
    reinf_texts = [t for t in col_cells.get('reinforcement', []) if t.strip()]
    reinforcement_text = reinf_texts[0] if reinf_texts else None

    # Shape dims: collect numeric values from cells in the x band between bar_mark and
    # reinforcement columns.  Handles both pilecap (unlabeled merged header) and pile/pier
    # (sub-headers "L1 m" / "L2 m").
    # Dedup key includes x position: same value at different x positions = different segments
    # (e.g. both 825mm hooks on bar 'a') → keep both.  Same value at the same x across
    # multi-zone rows = repeated annotation → deduplicate.
    # 50mm minimum excludes zone-number labels (1, 2) and other sub-column stray numerics.
    bm_col_x    = real_col_map.get('bar_mark')
    reinf_col_x = real_col_map.get('reinforcement')
    shape_dimensions = None
    if bm_col_x is not None and reinf_col_x is not None:
        seen: set = set()
        vals: list = []
        for row in bar_rows:
            for cell in row:
                if bm_col_x < cell['x'] < reinf_col_x:
                    v = _safe_float(cell['text'])
                    if v is not None and v >= 50:
                        key = (round(cell['x']), round(v))
                        if key not in seen:
                            seen.add(key)
                            vals.append(v)
        if vals:
            shape_dimensions = sorted(vals)

    return {
        'bar_mark':        bm,
        'component':       None,
        'reinforcement_text': reinforcement_text,
        'bar_dia_mm':      dia,
        'spacing_mm':      spacing,
        'count_text':      str(count_val) if count_val is not None else '',
        'count':           count_val,
        'length_m':        length_m,
        'total_length_m':  total_length_m,
        'unit_wt_kg_m':    unit_wt,
        'total_wt_kg':     total_wt_kg,
        'shape_dimensions': shape_dimensions,
    }


def _extract_schedule(msp, all_text: list, extents: tuple,
                      profile: DrawingTypeProfile, u2mm: float, diags: list) -> tuple:
    """
    Extract reinforcement schedule from DXF text entities.
    Returns (schedule dict, info dict). info = {'has_spacing_col': bool}.
    Every degradation path appends a diagnostic so failures are visible, not silent.
    """
    layout = profile.layout
    info = {'has_spacing_col': False}
    x_min, y_min, x_max, y_max = extents
    dw = x_max - x_min
    dh = y_max - y_min

    # The schedule sits in roughly the middle third of CASAD drawings.
    # Previous threshold of 50% cut through the column headers (DIA/NOS/LENGTH
    # at 45-49%, bar marks at 38%). Use 30% to capture the full schedule.
    sched_x_min = x_min + dw * layout.schedule_x_min_frac
    # Schedule cells are author-typed top-level TEXT. Block-derived text in this region
    # is symbol glyphs and annotation fragments (Ø symbols, attribute numbers) that land
    # in bar row ranges and corrupt column assignment — exclude it.
    in_region = [t for t in all_text if t['x'] >= sched_x_min]
    sched_text = [t for t in in_region if not t.get('from_block')]
    n_block_excluded = len(in_region) - len(sched_text)
    if n_block_excluded > 20 and len(sched_text) < n_block_excluded:
        # Mostly-block schedule region — a block-based schedule table would be invisible
        diags.append(diag('schedule_text_mostly_blocks',
                          f'{n_block_excluded} block-derived text entities in the schedule '
                          f'region were excluded vs {len(sched_text)} top-level entities kept. '
                          f'If this schedule is drawn as a block, extraction will be '
                          f'incomplete.', severity='info'))

    if not sched_text:
        log.warning('No text found in schedule area (x >= %.1f)', sched_x_min)
        diags.append(diag('schedule_area_empty',
                          'No text found in the schedule area (right portion of the sheet). '
                          'Schedule checks were skipped. If the schedule is positioned '
                          'elsewhere on this sheet, the layout assumption needs adjusting.'))
        return {}, info

    log.info('Schedule area: %d text entities at x >= %.1f (%.0f%% from left)',
             len(sched_text), sched_x_min, (sched_x_min - x_min) / dw * 100)

    rows = _group_rows(sched_text, tol_frac=layout.sched_row_tol_frac, extents=extents)
    if not rows:
        return {}, info

    # Find the column header row: must contain at least 2 of DIA / NOS / LENGTH.
    header_idx = None
    for idx, row in enumerate(rows):
        row_text = ' '.join(t['text'] for t in row).upper()
        if sum(1 for k in ('DIA', 'NOS', 'LENGTH') if k in row_text) >= 2:
            header_idx = idx
            break

    if header_idx is None:
        log.warning('Schedule column header row not found')
        diags.append(diag('schedule_header_not_found',
                          'The schedule column header row (DIA / NOS / LENGTH) was not found '
                          'in the DXF. Schedule checks were skipped.'))
        return {}, info

    # CASAD schedule headers span multiple Y-lines. Collect all sub-rows
    # within the header band of the identified header row.
    header_y = rows[header_idx][0]['y']
    header_sub_rows = [row for row in rows if abs(row[0]['y'] - header_y) < dh * layout.header_band_frac]

    col_map = _build_col_map(header_sub_rows)
    log.info('Schedule column map: %s', {k: f'x≈{v:.0f}' for k, v in col_map.items()})

    if not col_map:
        log.warning('Column map empty — cannot parse schedule rows')
        diags.append(diag('schedule_colmap_empty',
                          'Schedule column headers were found but none matched known column '
                          'keywords. Schedule checks were skipped.'))
        return {}, info

    info['has_spacing_col']    = 'spacing_mm' in col_map
    info['has_shape_dims_col'] = ('bar_mark' in col_map and 'reinforcement' in col_map)

    data_rows = rows[header_idx + 1:]
    if not data_rows:
        return {}, info

    # Detect component section boundaries (PILECAP / PILE / PIER header rows).
    # Primary method: structural headers in the schedule itself — works regardless
    # of what letters are used for bar marks.
    comp_boundaries = _build_comp_boundaries(data_rows, profile)
    use_boundaries = bool(comp_boundaries)
    if not use_boundaries:
        log.info('No component header rows found — falling back to bar-mark letter lookup')
        diags.append(diag('bar_mark_fallback_used',
                          'The schedule has no component sub-header rows; components were '
                          'assigned from bar mark letter conventions.', severity='info'))

    # Record DXF-extent-% positions of component header rows for coordinate calibration.
    # These match the same PILECAP/PILE/PIER text that pdfplumber finds in PDF-space.
    # The comparator uses the (dxf_y%, pdf_y%) pairs as anchor points to compute a linear
    # transform so that row_bbox y-coords (DXF model-space) can be mapped to PDF page coords.
    _dx = (x_max - x_min) or 1.0
    _dy = (y_max - y_min) or 1.0
    comp_anchors: dict = {}
    for _row_idx, _comp in comp_boundaries:
        if _comp in comp_anchors or _row_idx >= len(data_rows):
            continue
        _cells = data_rows[_row_idx]
        if not _cells:
            continue
        _cy = _cells[0]['y']
        _cx = _cells[0]['x']
        comp_anchors[_comp] = {
            'x': round((_cx - x_min) / _dx * 100, 2),
            'y': round((y_max - _cy)  / _dy * 100, 2),   # flipped: same as _to_bbox
        }
    info['dxf_comp_anchors'] = comp_anchors
    log.debug('DXF comp anchors: %s', comp_anchors)

    # Pass 1 — locate bar mark rows.
    # A bar mark is: any cell near the bar_mark column whose text is a single letter
    # optionally followed by 1-2 digits. No hardcoded list required — _is_bar_mark_token()
    # matches any letter convention (x/y/z, l/m/n, or anything else the project uses).
    bm_col_x = col_map.get('bar_mark')
    x_span = (max(col_map.values()) - min(col_map.values())) if len(col_map) > 1 else dw * 0.4
    bm_x_tol = x_span * 0.20  # bar mark column tolerance

    bar_positions: list[tuple[int, str]] = []  # (row_index_in_data_rows, bm)
    comp_header_rows: set[int] = {idx for idx, _ in comp_boundaries}

    for idx, row in enumerate(data_rows):
        if idx in comp_header_rows:
            continue  # skip component header rows — they're not bar data rows
        for cell in row:
            bm = cell['text'].strip().strip("'\"").lower()
            if not _is_bar_mark_token(bm):
                continue
            # If we have a bar_mark column, prefer cells near it; otherwise accept any
            if bm_col_x is None or abs(cell['x'] - bm_col_x) < bm_x_tol:
                bar_positions.append((idx, bm))
                break  # at most one bar mark per row

    if not bar_positions:
        log.warning('No bar marks found in schedule')
        diags.append(diag('no_bar_marks',
                          'A schedule header was found but no bar mark rows could be '
                          'identified. Schedule checks were skipped.'))
        return {}, info

    log.info('Pass 1: found %d bar marks: %s', len(bar_positions),
             [bm for _, bm in bar_positions])

    # Side-by-side schedule block detection threshold, converted from mm to drawing units.
    x_offset_min = layout.table_offset_min_mm / u2mm

    # Pass 2 — for each bar mark, assign rows in its range (midpoint between
    # adjacent bar marks) and aggregate all data found there.
    schedule: dict = {}
    dropped: list = []   # (bar mark, reason) — surfaced as a diagnostic, not just a log line
    n = len(data_rows)

    for pos_i, (bm_row_idx, bm) in enumerate(bar_positions):
        # Component: from structural boundary (preferred) or letter lookup (fallback)
        if use_boundaries:
            comp = _comp_for_row(bm_row_idx, comp_boundaries)
            if comp is None:
                log.debug('Bar %r at row %d precedes all component headers — skipping', bm, bm_row_idx)
                dropped.append((bm, 'appears above the first component header'))
                continue
        else:
            comp = profile.bar_mark_comp_fallback.get(bm)
            if comp is None:
                log.debug('Bar mark %r not in letter-convention fallback — skipping', bm)
                dropped.append((bm, 'not in the bar-mark letter conventions for this drawing type'))
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
        bar_data = _aggregate_bar_rows(bar_rows, col_map, bm, x_offset_min)
        if bar_data is None:
            continue

        # Attach exact DXF bbox for this bar's rows so the review UI can highlight
        # the precise schedule row(s) instead of distributing evenly within a section.
        # bar_rows spans the bar mark label row plus any sub-rows (confinement zones).
        if bar_rows:
            all_ys = [cell['y'] for row in bar_rows for cell in row]
            all_xs = [cell['x'] for row in bar_rows for cell in row]
            if all_ys and all_xs:
                row_pad = dh * layout.sched_row_tol_frac
                bar_data['row_bbox'] = _to_bbox(
                    min(all_xs) - x_span * 0.05,
                    min(all_ys) - row_pad,
                    max(all_xs) + x_span * 0.05,
                    max(all_ys) + row_pad,
                    extents,
                )

        bar_data['bar_mark'] = bm
        comp_dict = schedule.setdefault(comp, {})
        if bm in comp_dict:
            # Bar mark seen again — second schedule block for a different pier variant.
            # The anchor-based aggregation already captures multi-zone bars (y, y1, i…)
            # within one occurrence, so any second appearance is a genuinely separate
            # block and should not double-count into the primary schedule.
            log.debug('Bar mark %r already recorded — skipping second-block occurrence', bm)
            dropped.append((bm, 'duplicate occurrence (second schedule block) not aggregated'))
        else:
            comp_dict[bm] = bar_data

    if dropped:
        detail = '; '.join(f"'{bm}' ({reason})" for bm, reason in dropped)
        diags.append(diag('schedule_bars_dropped',
                          f'{len(dropped)} schedule row(s) were skipped during extraction and '
                          f'were NOT checked: {detail}.', severity='info'))

    log.info('Schedule parsed: %s', {c: list(b.keys()) for c, b in schedule.items()})
    return schedule, info


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

# ATTRIB default/placeholder values that the CASAD title block template ships with.
# When an engineer leaves a field blank, the block retains its default prompt text
# (e.g. "DRAWN BY") as the ATTRIB value.  Treat these as absent.
_TITLE_PLACEHOLDER_VALS = frozenset({
    'DRAWN BY', 'DRAWN', 'APPROVED BY', 'APPROVED', 'DESIGN BY',
    'DESIGNED BY', 'CHECKED BY', 'CHECKED', 'NAME', 'INITIALS',
    'SIGNATURE', 'DATE', 'SCALE', 'REVISION', 'REV', 'TITLE',
    'DRAWING NO', 'DRAWING NUMBER', 'DRG NO', 'SPAN', 'SPANS',
})


def _extract_title_block(doc, msp, all_text: list, extents: tuple,
                         profile: DrawingTypeProfile = PPP_PROFILE,
                         ps_text: list = None) -> dict:
    """
    Extract title block fields. Tries ATTRIB entities first, then text patterns in the
    bottom-right quadrant of modelspace, then the same patterns over paperspace text
    (title blocks often live in a paperspace layout, not modelspace).
    """
    result = {}

    # Pass 1: INSERT blocks with ATTRIB entities (standard CASAD template)
    try:
        for insert in msp.query('INSERT'):
            for attrib in insert.attribs:
                tag = (attrib.dxf.tag or '').upper().strip()
                val = (attrib.dxf.text or '').strip()
                if not val or val.upper() in _TITLE_PLACEHOLDER_VALS:
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
    layout = profile.layout
    x_min, y_min, x_max, y_max = extents
    tb_x_min = x_min + (x_max - x_min) * layout.title_x_min_frac
    tb_y_max = y_min + (y_max - y_min) * layout.title_y_max_frac

    tb_text = [t for t in all_text if t['x'] >= tb_x_min and t['y'] <= tb_y_max]
    _title_block_pattern_pass(tb_text, result)

    # Pass 3: paperspace fallback — when modelspace yielded little, run the same
    # patterns over paperspace text (quadrant-filtered against the paperspace's own
    # text-cloud extents, since paperspace coordinates are unrelated to modelspace).
    if ps_text and sum(1 for v in result.values() if v) < 3:
        xs = [t['x'] for t in ps_text]
        ys = [t['y'] for t in ps_text]
        ps_x_min = min(xs) + (max(xs) - min(xs)) * layout.title_x_min_frac
        ps_y_max = min(ys) + (max(ys) - min(ys)) * layout.title_y_max_frac
        ps_tb = [t for t in ps_text if t['x'] >= ps_x_min and t['y'] <= ps_y_max] or ps_text
        before = sum(1 for v in result.values() if v)
        _title_block_pattern_pass(ps_tb, result)
        added = sum(1 for v in result.values() if v) - before
        if added:
            log.info('Title block: %d field(s) filled from paperspace text', added)

    # Drawing title: search modelspace then paperspace for the profile's title patterns
    for t in list(all_text) + list(ps_text or []):
        if result.get('title'):
            break
        if any(p in t['text'].upper() for p in profile.title_patterns):
            result['title'] = t['text'].strip()

    result['bbox'] = None  # position data comes from pdfplumber merge
    return result


def _title_block_pattern_pass(tb_text: list, result: dict):
    """Run the title-block field regexes over a text list, filling unset fields in place."""
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


# ── Notes extraction ──────────────────────────────────────────────────────────

def _extract_notes(all_text: list, extents: tuple,
                   profile: DrawingTypeProfile = PPP_PROFILE) -> dict:
    """
    Extract engineering note values. Which values, and how to find them, is defined
    by the profile (note_float_patterns / note_string_patterns / components) — this
    function carries no drawing-type knowledge of its own.
    """
    layout = profile.layout
    notes = {}

    # Find the NOTES section label — use startswith so "NOTES:", "GENERAL NOTES :" match.
    notes_anchor = None
    for t in all_text:
        tu = t['text'].strip().upper()
        if any(tu.startswith(kw) for kw in ('NOTES', 'NOTE', 'GENERAL NOTES')):
            notes_anchor = t
            break

    # Scan all text (full drawing) or just near NOTES anchor
    x_min, y_min, x_max, y_max = extents
    if notes_anchor:
        dy = (y_max - y_min) * layout.notes_h_frac
        scan = [t for t in all_text
                if abs(t['x'] - notes_anchor['x']) < (x_max - x_min) * layout.notes_w_frac
                and (notes_anchor['y'] - dy) <= t['y'] <= notes_anchor['y'] + 5]
    else:
        # Fall back: scan the views (left) portion of the drawing
        scan = [t for t in all_text if t['x'] < x_min + (x_max - x_min) * layout.views_x_max_frac]

    # Build full_text preserving per-line structure using DXF y-coordinates.
    # ' '.join() produces one flat string with no \n, so split('\n') was a no-op.
    # Real line breaks let the grade extraction loop correctly exclude LAP-table lines.
    rows = _group_rows(scan, tol_frac=0.005, extents=extents)
    full_text = '\n'.join(
        ' '.join(t['text'] for t in sorted(r, key=lambda t: t['x']))
        for r in rows
    )
    full_upper = full_text.upper()
    log.info('Notes scan: anchor=%r rows=%d text_preview=%r',
             notes_anchor and notes_anchor['text'], len(rows), full_text[:300])

    # Numeric note values (pile length, fixity, dia, …) — first non-None capture group
    for key, pattern in profile.note_float_patterns.items():
        m = re.search(pattern, full_upper)
        if m:
            val = next((g for g in m.groups() if g is not None), None)
            if val is not None:
                notes[key] = _safe_float(val)

    # String note values (steel grade, lap-length concrete grade, …)
    for key, pattern in profile.note_string_patterns.items():
        m = re.search(pattern, full_upper)
        if m:
            notes[key] = re.sub(r'\s+', '', m.group(1))

    # Concrete grades — "M35 PILECAP" assigns to that component; a grade with no
    # component qualifier (or whose component is already set) applies to all unset ones.
    # Only the profile's known grades match — a bare \bM\d+\b would pick up grid
    # labels like "M1" from annotation text.
    # IMPORTANT: skip lines that contain "LAP" or "OVERLAP" — the lap length table header
    # (e.g. "LAP LENGTHS FOR M35 CONCRETE") has no component qualifier, so the grade would
    # fall into the "else: set all components" branch, making concrete_pile=M35 from the
    # lap table — identical to lap_length_concrete_grade — so the comparison always matches.
    grade_re = re.compile(r'\b(' + '|'.join(profile.concrete_grade_keywords) + r')\b')
    _lap_re  = re.compile(r'\b(LAP|OVERLAP)\b')
    comps_desc = profile.comps_longest_first()   # longest first: PILECAP before PILE
    for line in full_text.split('\n'):
        lu = line.upper()
        if _lap_re.search(lu):
            continue   # skip lap-table lines — grade here is the lap reference, not a component spec
        m = grade_re.search(lu)
        if not m:
            continue
        grade = m.group(1)
        target = next((c for c in comps_desc if c.upper() in lu), None)
        key = f'concrete_{target}' if target else None
        if key and key not in notes:
            notes[key] = grade
        else:
            for comp in profile.components:
                notes.setdefault(f'concrete_{comp}', grade)

    log.info('Notes extracted: %s', {k: v for k, v in notes.items() if k != 'bbox'})
    notes['bbox'] = None
    return notes


def _append_section_grade_diagnostics(all_text: list, extents: tuple,
                                       profile: DrawingTypeProfile,
                                       notes: dict, diags: list):
    """
    For each section view whose label unambiguously names ONE component, search for
    a concrete grade annotation (M30–M50) within the estimated view height below the
    label.  If the found grade differs from the notes grade for that component, append
    an error diagnostic.

    Conservative: only fires for single-component sections to avoid false positives on
    mixed labels like "SECTION A-A FOR PILE PILECAP AND PIER".
    """
    x_min, y_min, x_max, y_max = extents
    dh = y_max - y_min
    view_h = dh * profile.layout.view_h_frac

    grade_re = re.compile(r'\b(' + '|'.join(profile.concrete_grade_keywords) + r')\b')
    all_rows = _group_rows(all_text, tol_frac=profile.layout.row_tol_frac, extents=extents)
    comps_longest = profile.comps_longest_first()  # PILECAP before PILE so substring match works

    seen = set()   # avoid duplicate diagnostics for the same section
    for row in all_rows:
        line = ' '.join(t['text'] for t in sorted(row, key=lambda t: t['x'])).upper().strip()
        if 'SECTION' not in line:
            continue
        comps_in = [c for c in comps_longest if c.upper() in line]
        if len(comps_in) != 1:
            continue  # mixed or unknown component — skip
        comp = comps_in[0]
        expected = (notes.get(f'concrete_{comp}') or '').upper()
        if not expected:
            continue

        y_label = row[0]['y']         # DXF Y (bottom-up)
        y_low   = y_label - view_h    # lower bound of view region

        for t in all_text:
            if not (y_low <= t['y'] <= y_label):
                continue
            # Allow from_block items only when they contain a grade keyword —
            # ATTRIB blocks in section annotations carry grade text like "(M35)".
            if t.get('from_block') and not grade_re.search(t['text'].upper()):
                continue
            m = grade_re.search(t['text'].upper())
            if not m:
                continue
            found = m.group(1)
            if found == expected:
                break   # correct grade — no issue
            key = (line[:60], found)
            if key not in seen:
                seen.add(key)
                diags.append(diag(
                    'section_grade_mismatch',
                    f'Section "{line[:60]}" shows {found} concrete but notes specify '
                    f'{expected} for {comp}.',
                    severity='error'
                ))
            break


# ── Geometric spatial classification (Tier 2) ────────────────────────────────

_EXCLUDED_LAYER_KEYWORDS = ('REINF', 'REBAR', 'TEXT', 'SHAPE_BAR', 'SHAPE BAR')

# Plausible real-world component size bounds, in mm — independent of sheet layout.
# A full production sheet's extents are dominated by far-apart title block/schedule/
# multiple section views, so sizing thresholds as a fraction of *sheet* extents (the
# old approach) silently rejects real components on full sheets while only working
# by coincidence on single-section crops where sheet size ≈ component size. Absolute
# mm bounds (converted via u2mm, per the codebase's existing units convention) don't
# have this failure mode.
_PILECAP_MIN_W_MM, _PILECAP_MAX_W_MM = 1000.0, 15000.0
_PILECAP_MIN_H_MM = 200.0
_PIER_MIN_W_MM, _PIER_MAX_W_MM = 300.0, 6000.0
_PIER_MIN_H_MM, _PIER_MAX_H_MM = 300.0, 6000.0
_PILE_MIN_R_MM, _PILE_MAX_R_MM = 150.0, 3000.0

# A pier pedestal/haunch drawn atop a pilecap in the same section view is itself
# wide-flat-shaped and falls within the pilecap size bounds, so it would otherwise
# register as its own independent pilecap candidate — confirmed on a real production
# sheet where a 2100x900mm pedestal sitting on a real 4500x1800mm pilecap pulled a
# pier-width DIMENSION onto 'pilecap_width' instead of 'pier_plan_dim'. Suppressed via
# _suppress_nested_pilecap_candidates: a candidate is dropped when its x-range is
# contained within a larger candidate's x-range and the two bboxes are vertically
# stacked directly against each other.
_PILECAP_NESTED_TOL_MM = 100.0


def _suppress_nested_pilecap_candidates(candidates: list, u2mm: float = 1.0) -> list:
    """
    Drop a pilecap candidate whose x-range sits inside a larger candidate's x-range
    and whose bbox is vertically stacked directly against it — see _PILECAP_NESTED_TOL_MM.
    """
    tol = _PILECAP_NESTED_TOL_MM / u2mm if u2mm else _PILECAP_NESTED_TOL_MM
    survivors = []
    for cand in candidates:
        x0, y0, x1, y1 = cand['bbox']
        nested = False
        for other in candidates:
            if other is cand or other['width'] <= cand['width']:
                continue
            ox0, oy0, ox1, oy1 = other['bbox']
            contained_x = ox0 - tol <= x0 and x1 <= ox1 + tol
            stacked = abs(y0 - oy1) <= tol or abs(y1 - oy0) <= tol
            if contained_x and stacked:
                nested = True
                break
        if not nested:
            survivors.append(cand)
    return survivors


def _detect_component_regions(msp, extents: tuple, u2mm: float = 1.0,
                               view_labels: list | None = None) -> dict:
    """
    Infer structural component bounding boxes from LWPOLYLINE and ARC geometry.
    Returns {name: {type, bbox=(xmin,ymin,xmax,ymax), center?, radius?, width?, height?}}.

    Classification heuristics (all size bounds in absolute mm via u2mm — see
    _PILECAP_*/_PIER_*/_PILE_* constants — not fractions of whole-sheet extents,
    which break down on full multi-view sheets):
      - Wide flat polyline on non-REINF/TEXT layer, within pilecap size bounds → pilecap
      - Near-square polyline within pier size bounds → pier (detected independently,
        not paired to a specific pilecap — real CASAD sheets often draw the pier
        cross-section in its own separate named view, spatially unrelated to the
        pilecap+pier combined view)
      - ARC with radius in a plausible pile-size range → pile circle

    Multiple matches per type are expected on full sheets (the same physical
    component is typically shown via more than one section cut) and are all
    returned, indexed pilecap_0/pilecap_1/..., pier_0/pier_1/..., pile_0/pile_1/...
    — callers should cross-check rather than assume a single instance.

    view_labels (from _extract_view_labels) gates out candidates whose nearest view
    label is a plan or detail view — a plan-view footprint (e.g. "PLAN OF PILECAP")
    can be similarly shaped/sized to a real section profile and would otherwise be
    misread as that component's section dimension. Also gates out candidates farther
    than _MAX_LABEL_DIST_MM from every label ('unplaced') — e.g. a small ARC bend from
    a bar-shape sketch in the schedule table, which would otherwise nearest-match
    whichever named view happens to be least far away even when that's 10+m off.
    None/empty view_labels → no gating (preserves behavior on label-less cropped DXFs).
    """
    x_min, y_min, x_max, y_max = extents
    dw = (x_max - x_min) or 1.0
    dh = (y_max - y_min) or 1.0
    regions: dict = {}

    def _layer_excluded(layer: str) -> bool:
        layer = layer.upper()
        return any(kw in layer for kw in _EXCLUDED_LAYER_KEYWORDS)

    pilecap_candidates = []
    pier_idx = 0
    for poly in msp.query('LWPOLYLINE'):
        try:
            pts = list(poly.get_points())
            if len(pts) < 3:
                continue
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
            w = max(xs) - min(xs)
            h = max(ys) - min(ys)
            if w <= 0 or h <= 0:
                continue
            layer = str(poly.dxf.get('layer', ''))
            if _layer_excluded(layer):
                continue
            # Sheet border / extents rectangle — exact match to whole-sheet size.
            if w > dw * 0.95 and h > dh * 0.95:
                continue

            w_mm, h_mm = w * u2mm, h * u2mm
            bbox = (min(xs), min(ys), max(xs), max(ys))
            cx = (min(xs) + max(xs)) / 2
            cy = (min(ys) + max(ys)) / 2
            aspect = w / h if h > 0 else 0

            # Wide flat shape → pilecap.
            if (aspect > 1.5 and _PILECAP_MIN_W_MM <= w_mm <= _PILECAP_MAX_W_MM
                    and h_mm >= _PILECAP_MIN_H_MM):
                if _nearest_label_view_type(cx, cy, view_labels, u2mm) in ('plan', 'detail', 'unplaced'):
                    continue
                pilecap_candidates.append({
                    'bbox': bbox, 'center': (cx, cy), 'width': w, 'height': h,
                })
                continue

            # Near-square shape within pier size bounds → pier. Detected independently
            # of pilecap position (see docstring) — aspect tolerance widened to admit
            # genuinely square piers (aspect == 1.0 exactly is common and must not be
            # excluded by a strict aspect < 1.0 check).
            if (0.6 <= aspect <= 1.6
                    and _PIER_MIN_W_MM <= w_mm <= _PIER_MAX_W_MM
                    and _PIER_MIN_H_MM <= h_mm <= _PIER_MAX_H_MM):
                if _nearest_label_view_type(cx, cy, view_labels, u2mm) in ('plan', 'detail', 'unplaced'):
                    continue
                regions[f'pier_{pier_idx}'] = {
                    'type': 'pier', 'bbox': bbox,
                    'center': (cx, cy), 'width': w, 'height': h,
                }
                pier_idx += 1
        except Exception:
            pass

    for i, cand in enumerate(_suppress_nested_pilecap_candidates(pilecap_candidates, u2mm)):
        regions[f'pilecap_{i}'] = {'type': 'pilecap', **cand}

    pile_idx = 0
    for arc in msp.query('ARC'):
        try:
            r = float(arc.dxf.radius)
            r_mm = r * u2mm
            if r_mm < _PILE_MIN_R_MM or r_mm > _PILE_MAX_R_MM:
                continue
            c = arc.dxf.center
            cx, cy = float(c.x), float(c.y)
            if _nearest_label_view_type(cx, cy, view_labels, u2mm) in ('plan', 'detail', 'unplaced'):
                continue
            regions[f'pile_{pile_idx}'] = {
                'type': 'pile',
                'center': (cx, cy),
                'radius': r,
                'bbox': (cx - r, cy - r, cx + r, cy + r),
            }
            pile_idx += 1
        except Exception:
            pass

    log.info('Component regions detected: %s', {k: v['type'] for k, v in regions.items()})
    return regions


_BUNDLE_TOL_MM = 800.0   # max gap between bundle-pile centers; real distinct piles sit farther apart


def _group_pile_groups(piles: dict, u2mm: float = 1.0) -> list:
    """
    Cluster pile circles by x-position — bundle piles share approximately the same x.
    Uses an absolute mm tolerance (bundle piles are typically <800mm apart; distinct
    pile lines are spaced at least ~1.5-2x pile diameter, i.e. well over 1m) rather
    than a fraction of drawing width — on a full sheet, drawing width is dominated by
    far-apart title block/schedule/other views and a width-relative tolerance would
    incorrectly merge genuinely distinct piles into one group.
    Returns sorted list of group centre x-values.
    """
    if not piles:
        return []
    xs = sorted(v['center'][0] for v in piles.values())
    tol = _BUNDLE_TOL_MM / u2mm if u2mm else _BUNDLE_TOL_MM
    groups: list = []
    current = [xs[0]]
    for x in xs[1:]:
        if x - current[-1] <= tol:
            current.append(x)
        else:
            groups.append(sum(current) / len(current))
            current = [x]
    groups.append(sum(current) / len(current))
    return sorted(groups)


_DIM_SNAP_TOL_MM = 150.0   # absolute snap tolerance for defpoint-to-edge matching


def _classify_dim_spatially(dp2, dp3, val_mm: float, orient: str,
                             regions: dict, u2mm: float = 1.0) -> tuple:
    """
    Map one DIMENSION (by its defpoints) to a named geometric parameter.
    Tests against every detected pilecap_i/pier_i candidate (a full sheet may show
    the same physical component via more than one section cut) and returns on the
    first snap match. Returns (param_name, component_key) or ('unknown', None).
    """
    tol = _DIM_SNAP_TOL_MM / u2mm if u2mm else _DIM_SNAP_TOL_MM

    def near(a, b):
        return abs(a - b) <= tol

    pilecaps = [(k, v) for k, v in regions.items() if v['type'] == 'pilecap']
    piers = [(k, v) for k, v in regions.items() if v['type'] == 'pier']
    piles = {k: v for k, v in regions.items() if v['type'] == 'pile'}

    for key, pilecap in pilecaps:
        pc = pilecap['bbox']   # xmin, ymin, xmax, ymax

        if orient == 'V':
            lo_y, hi_y = sorted([float(dp2.y), float(dp3.y)])
            if near(lo_y, pc[1]) and near(hi_y, pc[3]):
                return 'pilecap_depth', key

        if orient == 'H':
            lo_x, hi_x = sorted([float(dp2.x), float(dp3.x)])
            if near(lo_x, pc[0]) and near(hi_x, pc[2]):
                return 'pilecap_width', key
            pc_w = pc[2] - pc[0]
            if (hi_x - lo_x) > pc_w * 1.05:
                return 'pilecap_length_overall', key

    if orient == 'H':
        for key, pier in piers:
            pr = pier['bbox']
            lo_x, hi_x = sorted([float(dp2.x), float(dp3.x)])
            if near(lo_x, pr[0]) and near(hi_x, pr[2]):
                # Single section view shows only one plan direction of the pier — could
                # be length (along traffic) or width (across traffic) depending on which
                # section this is. Reported generically; comparator checks it against
                # whichever design dimension is closer.
                return 'pier_plan_dim', key

    if orient == 'H' and len(piles) >= 2:
        pile_groups = _group_pile_groups(piles, u2mm)
        lo_x, hi_x = sorted([float(dp2.x), float(dp3.x)])
        for i, g1 in enumerate(pile_groups):
            for g2 in pile_groups[i + 1:]:
                if near(lo_x, g1) and near(hi_x, g2):
                    return 'pile_spacing', 'pile'
        for key, pilecap in pilecaps:
            pc = pilecap['bbox']
            outermost_l = pile_groups[0]
            outermost_r = pile_groups[-1]
            if (near(lo_x, outermost_r) and near(hi_x, pc[2])) or \
               (near(lo_x, pc[0]) and near(hi_x, outermost_l)):
                return 'pile_overhang', key

    for key, pile in piles.items():
        cx, cy, r = pile['center'][0], pile['center'][1], pile['radius']
        if orient == 'V':
            lo_y, hi_y = sorted([float(dp2.y), float(dp3.y)])
            if near(lo_y, cy - r) and near(hi_y, cy + r):
                return 'pile_dia', key

    return 'unknown', None


def _classify_all_dims(msp, all_text: list, extents: tuple,
                        profile: DrawingTypeProfile = PPP_PROFILE, u2mm: float = 1.0) -> dict:
    """
    Classify DIMENSION entities spatially using defpoint-to-geometry matching.
    Skips dims that already have a text override (handled by _extract_dimensions).
    Returns geometry_from_drawing: {param → [{val_mm, x_pct, y_pct, component, source}, ...]}.
    Always a list — a full sheet may show the same physical component via more than
    one section cut, each contributing an independent reading to be cross-checked.

    view_labels (from _extract_view_labels) excludes plan/detail-view candidates from
    _detect_component_regions before any DIMENSION gets snapped to them — see that
    function's docstring for why plan-view footprints would otherwise be misread as
    section-profile dimensions.
    """
    view_labels = _extract_view_labels(all_text, profile)
    regions = _detect_component_regions(msp, extents, u2mm, view_labels)
    result: dict = {}
    seen_keys: set = set()   # (param, component_key) — dedup repeated dims for the same candidate
    total = 0
    classified = 0

    for e in msp.query('DIMENSION'):
        total += 1
        try:
            val_mm = float(e.get_measurement()) * u2mm
            if val_mm <= 0 or val_mm > 1e6:
                continue

            override = e.dxf.get('text', '') or ''
            if _strip_dim_override(override).strip():
                continue   # labeled dim — already handled by _extract_dimensions

            dp2 = e.dxf.get('defpoint2', None)
            dp3 = e.dxf.get('defpoint3', None)
            if dp2 is None or dp3 is None:
                continue

            dx = abs(float(dp3.x) - float(dp2.x))
            dy = abs(float(dp3.y) - float(dp2.y))
            if dx > dy * 2:
                orient = 'H'
            elif dy > dx * 2:
                orient = 'V'
            else:
                continue   # diagonal — skip

            try:
                tm = e.dxf.text_midpoint
                x_pct, y_pct = _pos_to_pct(float(tm.x), float(tm.y), extents)
            except AttributeError:
                mx = (float(dp2.x) + float(dp3.x)) / 2
                my = (float(dp2.y) + float(dp3.y)) / 2
                x_pct, y_pct = _pos_to_pct(mx, my, extents)

            param, comp_key = _classify_dim_spatially(dp2, dp3, val_mm, orient, regions, u2mm)
            if param == 'unknown':
                continue

            dedup_key = (param, comp_key)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            result.setdefault(param, []).append({
                'val_mm':    round(val_mm, 1),
                'x_pct':     x_pct,
                'y_pct':     y_pct,
                'component': regions.get(comp_key, {}).get('type', comp_key),
                'source':    'dxf_spatial',
            })
            classified += 1
        except Exception as ex:
            log.debug('Spatial dim classify error: %s', ex)

    log.info('Spatial dim classification: %d/%d DIMENSION entities classified → %s',
             classified, total, {k: len(v) for k, v in result.items()})
    return result


def _extract_multileader_callouts(msp, extents: tuple) -> list:
    """
    Extract bar mark annotations from MULTILEADER entities.
    Text is in the MLeader context MTEXT, via entity.context.mtext.default_content.
    Returns [{bar_mark, x_pct, y_pct}].
    """
    callouts = []
    for e in msp.query('MULTILEADER'):
        try:
            ctx = e.context
            if not ctx or not ctx.mtext:
                continue
            raw = (ctx.mtext.default_content or '').strip()
            if not raw:
                continue
            # Strip MTEXT formatting codes
            bar_mark = re.sub(r'\\[A-Za-z][^;]*;', '', raw)
            bar_mark = re.sub(r'\{[^}]*\}', '', bar_mark).strip().lower()
            if not re.match(r'^[a-z]\d{0,2}$', bar_mark):
                continue
            pos = ctx.mtext.insert
            x_pct, y_pct = _pos_to_pct(float(pos[0]), float(pos[1]), extents)
            callouts.append({'bar_mark': bar_mark, 'x_pct': x_pct, 'y_pct': y_pct})
        except Exception as ex:
            log.debug('MULTILEADER parse error: %s', ex)
    log.info('MULTILEADER callouts: %d extracted', len(callouts))
    return callouts


# ── DIMENSION entity extraction ───────────────────────────────────────────────

def _strip_dim_override(text: str) -> str:
    """Strip AutoCAD MTEXT formatting codes from a DIMENSION override string.

    {\\Wn;text} braces are a formatting *scope*, not a content wrapper — a
    bar-count callout written as {\\W1;32 -15 NOS} must yield "32 -15 NOS", not ''.
    Strip the \\code; prefix first, then strip the brace delimiters themselves
    (never delete the text they enclose) before stripping the other codes below.
    """
    if not text:
        return ''
    text = re.sub(r'\\[A-Za-z][^;\\]*?;', '', text)  # \code; inline codes (incl. inside braces)
    text = text.replace('{', '').replace('}', '')      # formatting-scope delimiters only
    text = re.sub(r'\\[Xx]', ' ', text)          # \X stacking separator
    text = re.sub(r'\\[Pp]', ' ', text)          # \P paragraph break
    text = text.replace('<>', '').strip()         # <> = "show measured value"
    return re.sub(r'\s{2,}', ' ', text).strip()


def _extract_dimensions(msp, extents: tuple, u2mm: float = 1.0) -> dict:
    """
    Extract DIMENSION entities from modelspace and classify them.
    Measured values are converted from drawing units to mm via u2mm.

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
            # get_measurement() returns drawing units — convert to mm via $INSUNITS factor
            val = float(e.get_measurement()) * u2mm
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

def _extract_table1(all_text: list, extents: tuple, layout=None) -> list:
    """Extract TABLE-1 pier elevation data."""
    layout = layout or PPP_PROFILE.layout
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

    # Collect text near TABLE-1: within the scan window around/below the label
    nearby = [t for t in all_text
              if abs(t['x'] - table1_anchor['x']) < dw * layout.table1_w_frac
              and (table1_anchor['y'] - dh * layout.table1_h_frac) <= t['y'] <= table1_anchor['y'] + 5]

    rows = _group_rows(nearby, tol_frac=layout.sched_row_tol_frac, extents=extents)
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


# ── View label extraction (for Tier 2 plan/section disambiguation) ────────────

_VIEW_LABEL_RE = re.compile(r'^(SECTION|PLAN OF|REINFORCEMENT PLAN OF|DETAIL)\b', re.IGNORECASE)


def _extract_view_labels(all_text: list, profile: DrawingTypeProfile = PPP_PROFILE) -> list:
    """
    Identify view-title labels (SECTION x-x, PLAN OF ..., REINFORCEMENT PLAN OF ...,
    DETAIL x) and classify each by view_type and mentioned component(s).

    CASAD draws plan views of a component (e.g. "PLAN OF PILECAP") alongside section
    views (e.g. "SECTION A-A FOR PILECAP & PIER") on every sheet. A plan-view footprint
    can be similarly shaped/sized to a real section profile (e.g. a square pilecap plan
    vs. a square pier section) — shape/size alone can't tell them apart. This label list
    lets _detect_component_regions discard candidates that sit inside a plan/detail view,
    since none of the current geometry_checks represent plan-footprint measurements.

    Returns [{text, x, y, view_type, components}, ...] in raw model-space coordinates
    (same space as _detect_component_regions candidate centers). view_type is one of
    'section' / 'plan' / 'detail'. components is the set of component keywords
    (profile.comp_header_patterns) found in the label text — may be empty (e.g.
    "SECTION C-C" names no component explicitly).
    """
    labels = []
    for t in all_text:
        text = t['text'].strip()
        if not text or len(text) > 80:
            continue
        if not _VIEW_LABEL_RE.match(text):
            continue
        upper = text.upper()
        if upper.startswith('DETAIL'):
            view_type = 'detail'
        elif 'PLAN OF' in upper or 'REINFORCEMENT PLAN' in upper:
            view_type = 'plan'
        elif upper.startswith('SECTION'):
            view_type = 'section'
        else:
            continue
        components = {comp for comp, pat in profile.comp_header_patterns if pat.search(upper)}
        labels.append({
            'text': text, 'x': t['x'], 'y': t['y'],
            'view_type': view_type, 'components': components,
        })
    return labels


_MAX_LABEL_DIST_MM = 8000.0   # beyond this, a candidate isn't reliably "inside" any named
                              # view — e.g. a small ARC from a bar-shape sketch in the
                              # schedule table can otherwise get nearest-matched to whichever
                              # named view happens to be least far away, even 13+m off.
                              # Verified against a real production sheet: genuine view
                              # candidates sit within ~6000mm of their label; schedule-area
                              # debris sits 13000mm+ away — clean separation at 8000mm.


def _nearest_label_view_type(cx: float, cy: float, view_labels: list,
                              u2mm: float = 1.0) -> str | None:
    """
    Return the view_type of the nearest view label to (cx, cy):
      - None if view_labels is empty (label-less DXF, e.g. a cropped single-section
        extract — callers must treat None as "no filtering", not as a view type to discard).
      - 'unplaced' if the nearest label is farther than _MAX_LABEL_DIST_MM — too far to
        reliably belong to any view; callers should discard, same as 'plan'/'detail'.
      - otherwise the nearest label's view_type ('section' / 'plan' / 'detail').
    """
    if not view_labels:
        return None
    max_dist = _MAX_LABEL_DIST_MM / u2mm if u2mm else _MAX_LABEL_DIST_MM
    nearest = min(view_labels, key=lambda lb: (lb['x'] - cx) ** 2 + (lb['y'] - cy) ** 2)
    dist = ((nearest['x'] - cx) ** 2 + (nearest['y'] - cy) ** 2) ** 0.5
    if dist > max_dist:
        return 'unplaced'
    return nearest['view_type']


# ── Section info extraction ───────────────────────────────────────────────────

def _extract_section_info(all_text: list, extents: tuple, layout=None) -> tuple:
    """
    Return (section_view_positions, cut_letters).
    section_view_positions: {label_text: {x,y,w,h}} in PDF-style percentages.
    cut_letters: set of single uppercase letters appearing ≥2 times in left portion.
    """
    layout = layout or PPP_PROFILE.layout
    x_min, y_min, x_max, y_max = extents
    dw = x_max - x_min
    dh = y_max - y_min

    # Views area on the left (schedule occupies the right).
    # TRIGGER_WORDS like TABLE-1, SCHEDULE can appear in the right area too —
    # scan all text for labels but only count cut letters from the left area.
    view_x_max = x_min + dw * layout.views_x_max_frac

    all_rows = _group_rows(all_text, tol_frac=layout.row_tol_frac, extents=extents)

    section_view_positions = {}
    single_letter_counts = {}
    single_letter_ys     = {}  # letter → list of y-values, for axis-label filter

    for row in all_rows:
        row_sorted = sorted(row, key=lambda t: t['x'])
        line = ' '.join(t['text'] for t in row_sorted).upper().strip()
        # Normalise non-ASCII hyphens
        line = line.replace('\xad', '-').replace('–', '-').replace('—', '-')

        if any(tw in line for tw in TRIGGER_WORDS):
            x0  = min(t['x'] for t in row)
            x1  = max(t['x'] for t in row)
            y_c = row[0]['y']
            # Estimated view height below the label (matches pdfplumber heuristic)
            view_h = dh * layout.view_h_frac
            bbox = _to_bbox(x0, y_c, x1 + dw * 0.01, y_c - view_h, extents)
            section_view_positions[line[:80]] = bbox

        # Cut letter detection only in the left (view) area to avoid picking up
        # schedule row annotations, bar marks, pier labels on the right side.
        # Block-derived text is excluded EXCEPT ATTRIBs — ATTRIBs are user-typed
        # tag values on INSERT blocks (e.g. the "C"/"D" letter on a cut-mark arrow
        # block), not glyph substitutions from symbol block geometry.
        if all(t['x'] < view_x_max for t in row):
            for t in row:
                if t.get('from_block') and not t.get('is_attrib'):
                    continue
                s = t['text'].strip()
                if len(s) == 1 and s.isupper() and s.isalpha():
                    single_letter_counts[s] = single_letter_counts.get(s, 0) + 1
                    single_letter_ys.setdefault(s, []).append(t['y'])

    # Second pass over raw entities — cut marks are often separate entities that the
    # row grouping merges into mixed rows (same block-text exclusion applies)
    for t in all_text:
        if t['x'] < view_x_max and (not t.get('from_block') or t.get('is_attrib')):
            s = t['text'].strip()
            if len(s) == 1 and s.isupper() and s.isalpha():
                single_letter_counts[s] = single_letter_counts.get(s, 0) + 1
                single_letter_ys.setdefault(s, []).append(t['y'])

    # Cut-mark letters appear in PAIRS. Filter: if a letter appears ≥ 3 times all
    # within the same 5%-height horizontal band, it is a plan-view axis/grid label
    # (e.g., pier-column labels "C"/"D" repeated at each pier), not a cut mark pair.
    _y_band = dh * 0.05
    cut_letters = set()
    for letter, count in single_letter_counts.items():
        if count < 2:
            continue
        ys = single_letter_ys.get(letter, [])
        if count >= 3 and (max(ys) - min(ys)) <= _y_band:
            continue  # axis label, not a cut mark
        cut_letters.add(letter)

    log.info('Section info: %d labels, cut_letters=%s', len(section_view_positions), sorted(cut_letters))
    return section_view_positions, cut_letters


# ── Cross-section bar counting ────────────────────────────────────────────────

def _count_cross_section_bars(msp, all_text: list, schedule: dict, extents: tuple,
                              profile: DrawingTypeProfile = PPP_PROFILE,
                              diags: list = None, u2mm: float = 1.0) -> list:
    """
    Count bar-dot symbols within each section view to get exact bar counts.
    Returns list of cross_section_check dicts matching pdf_extractor format.

    Dot candidates, in priority order:
      1. CIRCLE entities (radius-filtered), preferring rebar-layer entities when the
         drawing's layers match profile.dot_layer_patterns
      2. Repeated block INSERTs — rebar dots are often a small block inserted per bar
    A section label with NO countable candidates produces an 'error' diagnostic
    instead of being skipped silently: "verified nothing" must never look like
    "verified correct".
    """
    if diags is None:
        diags = []
    layout = profile.layout
    x_min, y_min, x_max, y_max = extents
    dw = x_max - x_min
    dh = y_max - y_min

    # Collect all CIRCLE entities (with layer for rebar-layer preference)
    circles = []
    try:
        for e in msp.query('CIRCLE'):
            c = e.dxf.center
            circles.append({'x': float(c.x), 'y': float(c.y),
                            'r': float(e.dxf.radius),
                            'layer': str(e.dxf.layer or '')})
    except Exception as e:
        log.warning('CIRCLE query failed: %s', e)

    # Layer preference: when any circles sit on layers matching the profile's rebar
    # patterns, those are authoritative — restrict to them. Layer names are the
    # strongest semantic signal a DXF carries.
    layer_res = [re.compile(p, re.IGNORECASE) for p in profile.dot_layer_patterns]
    on_rebar_layer = [c for c in circles
                      if any(rx.search(c['layer']) for rx in layer_res)]
    if on_rebar_layer:
        log.info('Rebar-layer filter: %d of %d circles on matching layers (%s)',
                 len(on_rebar_layer), len(circles),
                 sorted({c['layer'] for c in on_rebar_layer}))
        circles = on_rebar_layer

    # Collect block INSERT points — rebar dots are usually a small named block
    # (e.g. CASAD's REIN.DOT) inserted once per bar, sometimes nested inside
    # anonymous *U group/array containers.
    insert_points = _collect_dot_insert_points(msp)
    # Blocks whose names match the profile's dot patterns are authoritative;
    # otherwise fall back to any block inserted often enough to be a per-bar symbol.
    name_res = [re.compile(p, re.IGNORECASE) for p in profile.dot_block_patterns]
    named_dots = {n: pts for n, pts in insert_points.items()
                  if any(rx.search(n) for rx in name_res)}
    dot_blocks = named_dots or {n: pts for n, pts in insert_points.items() if len(pts) >= 8}

    log.info('Dot candidates: %d circles, %d dot block(s) (%s)',
             len(circles), len(dot_blocks),
             {n: len(p) for n, p in dot_blocks.items()} or '-')

    # Find section labels in the views (left) area of the drawing
    view_x_max = x_min + dw * layout.views_x_max_frac
    results = []

    # Bare labels (e.g. "SECTION C-C" cut from a multi-component parent elevation like
    # "SECTION A-A FOR PILECAP & PIER") name no component of their own — resolve via
    # cut-mark geometry before falling back to skipping the count. See docstring.
    view_labels = _extract_view_labels(all_text, profile)
    regions = _detect_component_regions(msp, extents, u2mm, view_labels)
    cutmark_components = _infer_cutmark_components(all_text, view_labels, regions, profile)

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

        comp = _infer_component_from_label(label_text, profile)
        if comp == 'unknown':
            comp = cutmark_components.get(letter, 'unknown')
            if comp != 'unknown':
                diags.append(diag('xsec_component_from_cutmark',
                                  f'Section "{label_text}": component resolved as {comp!r} '
                                  f'via cut-mark position on its parent elevation '
                                  f'(label text itself names no component).',
                                  severity='info'))
        if comp == 'unknown':
            log.debug('Section %r: cannot determine component from label — skipping count', label_text)
            diags.append(diag('xsec_component_unknown',
                              f'Section "{label_text}": component could not be determined '
                              f'from the label text — bar count not verified.', severity='info'))
            continue
        bar_mark = _find_bar_mark_for_section(comp, schedule)

        # Search for bar dots in a region around the section label
        # (the section view is typically below and/or beside the label)
        search_x0 = lx - dw * layout.xsec_dx_frac
        search_x1 = lx + dw * layout.xsec_dx_frac
        search_y0 = ly - dh * layout.xsec_below_frac   # below label (DXF y decreases downward)
        search_y1 = ly + dh * layout.xsec_above_frac   # slightly above label

        def _in_box(c):
            return search_x0 <= c['x'] <= search_x1 and search_y0 <= c['y'] <= search_y1

        # Candidate source 1: dot-block insert points — drafter-placed bar symbols,
        # the strongest evidence when present. Hits are merged across all dot blocks
        # (CASAD drawings use both 'REIN.DOT' and 'REIN. DOT' for the same symbol).
        nearby = []
        if dot_blocks:
            block_hits = [p for pts in dot_blocks.values() for p in pts if _in_box(p)]
            if len(block_hits) >= 4:
                log.info('Section %s-%s: using %d dot-block insert points as bar dots',
                         letter, letter, len(block_hits))
                nearby = block_hits

        # Candidate source 2: circles, radius-filtered (excludes section boundary
        # circles) — for drawings that draw bar dots as plain CIRCLE entities.
        if not nearby:
            max_bar_r = dh * layout.dot_max_r_frac
            nearby = [c for c in circles if _in_box(c) and c['r'] <= max_bar_r]

        if not nearby:
            # 'info', not 'error': elevation sections (e.g. SECTION A-A FOR PILE)
            # legitimately contain no bar dots — an error here would falsely flag
            # every correct drawing. The record stays visible via the debug route.
            diags.append(diag('xsec_no_symbols',
                              f'Section {letter}-{letter} ({comp}): no bar symbols were '
                              f'recognised in the DXF near this view — the drawn bar count '
                              f'was not verified (expected for elevation views).',
                              severity='info'))
            continue

        # Cluster nearby dots: group dots that are close together into one section view
        cluster = _largest_cluster(nearby, max_gap=dw * layout.cluster_gap_frac)
        if not cluster:
            continue

        # Detect bundle bars (closely-spaced dot pairs) and collapse each pair into one
        # unit — the schedule bundle factor counts pairs, so visual_count must be in
        # pair-units, matching what the vision path counts.
        is_bundle = _detect_bundles(cluster)
        units = _collapse_bundle_pairs(cluster) if is_bundle else cluster

        bar_count = len(units)
        spacing_issues = _compute_spacing_issues(units) if bar_count >= 3 else []

        # Reliability guard: a single ring of exact DXF positions should be near-uniform.
        # Heavy irregularity means the cluster is not one bar group (e.g. a plan view
        # mixing several bar marks' dots) — report as unverifiable instead of flooding
        # the review with false count/spacing errors.
        if bar_count >= 8 and len(spacing_issues) > bar_count * 0.25:
            diags.append(diag('xsec_count_unreliable',
                              f'Section {letter}-{letter} ({comp}): {bar_count} bar symbols '
                              f'found but their arrangement is not a single uniform group — '
                              f'automatic count skipped. Verify this section manually.',
                              severity='info'))
            continue

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


def _collect_dot_insert_points(msp) -> dict:
    """
    Return {block_name: [{x, y, r, layer}]} for named block INSERTs, descending into
    anonymous '*' container blocks (groups/arrays) whose nested INSERTs are the actual
    placed symbols. Rebar dots are typically a named block (REIN.DOT) inserted per bar,
    sometimes wrapped in *U containers by ARRAY or GROUP operations.
    """
    points: dict = {}

    def walk(ins, depth: int):
        try:
            name = str(ins.dxf.name or '')
            if name.startswith('*'):
                # Anonymous container — recurse to find the real symbol inserts
                if depth < _MAX_BLOCK_DEPTH:
                    for ve in ins.virtual_entities():
                        if ve.dxftype() == 'INSERT':
                            walk(ve, depth + 1)
                return
            p = ins.dxf.insert
            points.setdefault(name, []).append(
                {'x': float(p.x), 'y': float(p.y), 'r': 0.0,
                 'layer': str(ins.dxf.layer or '')})
        except Exception:
            pass

    try:
        for ins in msp.query('INSERT'):
            walk(ins, 0)
    except Exception as e:
        log.debug('INSERT dot scan failed: %s', e)
    return points


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


def _collapse_bundle_pairs(cluster: list) -> list:
    """
    Merge closely-spaced dot pairs (bundle bars) into single units at the pair
    midpoint. The pair gap is taken as the smallest pairwise distance in the
    cluster; anything within 2.5× of it is treated as a pair. Unpaired dots
    pass through unchanged.
    """
    n = len(cluster)
    if n < 4:
        return cluster
    dists = []
    for i in range(n):
        for j in range(i + 1, n):
            d = math.hypot(cluster[i]['x'] - cluster[j]['x'],
                           cluster[i]['y'] - cluster[j]['y'])
            dists.append((d, i, j))
    dists.sort(key=lambda t: t[0])
    thresh = dists[0][0] * 2.5
    used: set = set()
    units = []
    for d, i, j in dists:
        if d > thresh:
            break
        if i in used or j in used:
            continue
        used.update((i, j))
        units.append({'x': (cluster[i]['x'] + cluster[j]['x']) / 2,
                      'y': (cluster[i]['y'] + cluster[j]['y']) / 2,
                      'r': max(cluster[i].get('r', 0), cluster[j].get('r', 0))})
    for k in range(n):
        if k not in used:
            units.append(cluster[k])
    return units


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


def _infer_component_from_label(label: str, profile: DrawingTypeProfile = PPP_PROFILE) -> str:
    """
    Infer the component from section label text using the profile's component names
    (longest first, so PILECAP matches before PILE). Returns 'unknown' if no component
    keyword is present — caller should skip counting rather than guess from section
    letter conventions. (Callers may still resolve 'unknown' via
    _infer_cutmark_components, which reads cut-mark geometry instead of guessing
    from the letter itself — see that function's docstring.)
    """
    u = label.upper()
    for comp in profile.comps_longest_first():
        if comp.upper() in u:
            return comp
    return 'unknown'


def _infer_cutmark_components(all_text: list, view_labels: list, regions: dict,
                               profile: DrawingTypeProfile = PPP_PROFILE) -> dict:
    """
    Resolve the component for SECTION X-X labels that name no component (e.g. bare
    "SECTION C-C") by reading where their cut-mark triangle sits on a parent elevation.

    CASAD convention: a multi-component elevation like "SECTION A-A FOR PILECAP & PIER"
    carries cut-mark triangle annotations (ATTRIB text "C", "D", ... on a small INSERT
    block) marking where child SECTION C-C / SECTION D-D views are cut from. Since the
    parent elevation already states both components, the child label omits the
    qualifier — it's implied by which part of the parent elevation the triangle sits on.
    Confirmed on a real production sheet (2026-06-22): cut-marks "C"/"D" for the pier
    cross-sections sit ~1500-2700mm *above* the matched pilecap region's top edge, i.e.
    in the pier shaft portion of the elevation, while the pilecap itself occupies the
    bbox below.

    Algorithm: for each unqualified "SECTION L-L" label, collect cut-mark ATTRIB
    occurrences of letter L, find the nearest *qualified* section label (by distance)
    to get the candidate component set, then use the cut-mark's y position relative to
    the nearest pilecap region's bbox to pick one: above the top edge → 'pier' (if pier
    is in the candidate set), inside the bbox → 'pilecap'. Returns {} (not 'unknown' —
    see _infer_component_from_label) for any letter where no pilecap region is nearby,
    where the candidate set has only one member already, or where the geometry doesn't
    resolve cleanly — under-detecting is safer than guessing.

    Returns {letter: component}.
    """
    unqualified = {}
    for lb in view_labels:
        if lb['view_type'] != 'section' or lb['components']:
            continue
        m = re.search(r'SECTION\s+([A-Z])[\-\xad–]\1', lb['text'].upper())
        if m:
            unqualified[m.group(1)] = lb

    qualified = [lb for lb in view_labels if lb['view_type'] == 'section' and lb['components']]
    pilecap_regions = [r for r in regions.values() if r['type'] == 'pilecap']
    if not unqualified or not qualified or not pilecap_regions:
        return {}

    cut_pts: dict = {}
    for t in all_text:
        s = t['text'].strip()
        if len(s) == 1 and s.isupper() and s.isalpha() and t.get('is_attrib') and s in unqualified:
            cut_pts.setdefault(s, []).append((t['x'], t['y']))

    result = {}
    for letter, pts in cut_pts.items():
        votes: dict = {}
        for cx, cy in pts:
            nearest_label = min(qualified, key=lambda lb: (lb['x'] - cx) ** 2 + (lb['y'] - cy) ** 2)
            candidates = nearest_label['components']
            if len(candidates) == 1:
                votes[next(iter(candidates))] = votes.get(next(iter(candidates)), 0) + 1
                continue
            if 'pilecap' not in candidates:
                continue   # geometry rule below is pilecap-relative; can't resolve otherwise
            nearest_pc = min(pilecap_regions,
                              key=lambda r: ((r['bbox'][0] + r['bbox'][2]) / 2 - cx) ** 2)
            _, pc_y0, _, pc_y1 = nearest_pc['bbox']
            if cy > pc_y1 and 'pier' in candidates:
                votes['pier'] = votes.get('pier', 0) + 1
            elif pc_y0 <= cy <= pc_y1:
                votes['pilecap'] = votes.get('pilecap', 0) + 1
        if votes:
            result[letter] = max(votes, key=votes.get)
            log.info('Cut-mark "%s" resolved to component %r via elevation geometry (votes=%s)',
                     letter, result[letter], votes)
    return result


def _find_bar_mark_for_section(comp: str, schedule: dict) -> str | None:
    """
    Return the primary bar mark to compare against circle count for a section view.
    Uses the first non-confinement bar in the component's extracted schedule.
    Confinement/ring bars have spacing_mm set; longitudinal/distributed bars don't.
    This works for any letter convention — no hardcoded names needed.
    """
    if comp not in schedule or not schedule[comp]:
        return None
    # First non-ring bar (no c/c spacing) is the longitudinal/distributed bar shown in section
    for bm, bar_data in schedule[comp].items():
        if not bar_data.get('spacing_mm'):
            return bm
    # All bars have spacing (unusual) — fall back to first bar
    return next(iter(schedule[comp]))


# ── Programmatic unlabeled-view and missing-detail detection ─────────────────

def _detect_unlabeled_section_circles(msp, all_text: list, extents: tuple,
                                       section_view_positions: dict,
                                       profile: DrawingTypeProfile = PPP_PROFILE,
                                       u2mm: float = 1.0) -> list:
    """
    Find large circular boundary shapes (pile / pilecap cross-section rings) in the views
    area that have no confirmed section-view label nearby.
    Returns [{description, bbox}] for unlabeled_views.

    Looks for both CIRCLE primitives AND closed LWPOLYLINEs with roughly circular aspect
    ratio — CASAD engineers most often draw pile sections as closed LWPOLYLINEs, not CIRCLE.

    Labelling is checked against `section_view_positions` (already-confirmed section labels)
    rather than raw TRIGGER_WORD text proximity.  TRIGGER_WORDS like 'LAP' and 'NOTES' appear
    near sections and caused false "already labeled" judgements in the previous approach.
    """
    x_min, y_min, x_max, y_max = extents
    dw = x_max - x_min
    dh = y_max - y_min
    view_x_max = x_min + dw * profile.layout.views_x_max_frac

    # Minimum radius for a "section boundary" shape.
    # 100mm absolute floor converted to drawing units via u2mm.
    # Rebar dots in CASAD DXFs are 5–20mm radius; pile sections are 300–600mm radius.
    min_sec_r = max(100.0 / u2mm, dh * 0.005)

    rings = []   # {x, y, r, kind}

    # --- CIRCLE primitives ---
    try:
        for e in msp.query('CIRCLE'):
            cx, cy, cr = float(e.dxf.center.x), float(e.dxf.center.y), float(e.dxf.radius)
            if cr >= min_sec_r and cx < view_x_max:
                rings.append({'x': cx, 'y': cy, 'r': cr, 'kind': 'circle'})
    except Exception:
        pass

    # --- Closed LWPOLYLINEs with roughly circular bounding box ---
    # Engineers draw pile cross-sections as closed polylines far more often than as CIRCLE.
    try:
        for e in msp.query('LWPOLYLINE'):
            if not e.is_closed:
                continue
            pts = list(e.get_points())
            if len(pts) < 6:   # triangles / quads are structure details, not circles
                continue
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
            bx_min, bx_max = min(xs), max(xs)
            by_min, by_max = min(ys), max(ys)
            w = bx_max - bx_min
            h = by_max - by_min
            if w < 1e-6 or h < 1e-6:
                continue
            # Roughly circular = aspect ratio within 15%
            if abs(w - h) / max(w, h) > 0.15:
                continue
            r_approx = (w + h) / 4   # average of half-width and half-height
            if r_approx < min_sec_r:
                continue
            cx = (bx_min + bx_max) / 2
            cy = (by_min + by_max) / 2
            if cx < view_x_max:
                rings.append({'x': cx, 'y': cy, 'r': r_approx, 'kind': 'lwpoly'})
    except Exception:
        pass

    log.info('Unlabeled-view scan: %d candidate rings (CIRCLE+LWPOLY, r≥%.0f) in views area',
             len(rings), min_sec_r)
    if not rings:
        return []

    # Check labelling by proximity to confirmed section-view labels in section_view_positions.
    # These were already extracted and confirmed by _extract_section_info — using them
    # avoids the false-positive problem where 'LAP', 'NOTES' TRIGGER_WORDS near the
    # sections counted as "labels" when they are not actually section titles.
    label_r = dh * 0.12   # 12% of sheet height — generous enough for labels above/below

    def _has_confirmed_label(c):
        for label, bbox in section_view_positions.items():
            # bbox is pct {x, y, width, height} from top-left — convert to DXF coords
            lx = x_min + bbox['x'] / 100 * dw
            ly = y_max - bbox['y'] / 100 * dh   # flip Y: PDF top-down → DXF bottom-up
            if abs(lx - c['x']) < label_r and abs(ly - c['y']) < label_r:
                return label
        return None

    # Deduplicate rings — a pile drawn as both a CIRCLE and a LWPOLYLINE approximation
    # would produce two entries at nearly the same position.  Keep the first one found.
    seen_positions = []
    unique_rings = []
    for c in rings:
        dup = any(abs(c['x'] - s['x']) < min_sec_r and abs(c['y'] - s['y']) < min_sec_r
                  for s in seen_positions)
        if not dup:
            unique_rings.append(c)
            seen_positions.append(c)

    unlabeled = []
    for c in unique_rings:
        x_pct = round((c['x'] - x_min) / dw * 100, 1)
        y_pct = round((y_max - c['y']) / dh * 100, 1)
        label = _has_confirmed_label(c)
        if label:
            log.info('Ring at %.1f%%,%.1f%% (r≈%.0f, %s) — confirmed label: %r',
                     x_pct, y_pct, c['r'], c['kind'], label[:60])
        else:
            bbox = _to_bbox(c['x'] - c['r'], c['y'] + c['r'],
                            c['x'] + c['r'], c['y'] - c['r'], extents)
            log.info('Unlabeled ring at %.1f%%,%.1f%% (r≈%.0f, %s) — no section label within %.0f units',
                     x_pct, y_pct, c['r'], c['kind'], label_r)
            unlabeled.append({
                'description': (
                    f'A structural cross-section ring is drawn at approximately '
                    f'{x_pct}% across, {y_pct}% down the drawing but has no '
                    f'"SECTION X-X" or similar title label nearby. '
                    f'Add a section title (e.g. "SECTION Z-Z FOR PILE") directly '
                    f'below or above this view.'
                ),
                'bbox': bbox,
            })
    return unlabeled


def _detect_missing_detail_refs(all_text: list,
                                 section_view_positions: dict) -> list:
    """
    If "DETAIL A" / "DETAILS A" callout text appears in the drawing but no
    corresponding view label "DETAIL A" exists in section_view_positions,
    flag as a missing referenced section.

    Returns [{cut_letter, found_on_view, missing_section, bbox}] suitable for
    _check_cut_mark_references in the comparator.
    """
    detail_re = re.compile(r'\bDETAIL[S]?\s+([A-Z])\b')

    refs: set = set()
    for t in all_text:
        # Allow block TEXT through here — "DETAIL A" inside a callout block is genuine
        # user text, not a glyph substitution like "O" for Ø.  The pattern is specific
        # enough (requires "DETAIL" + space + single letter) to avoid false matches.
        m = detail_re.search(t['text'].upper())
        if m:
            refs.add(m.group(1))

    if not refs:
        return []

    confirmed = ' '.join(section_view_positions.keys()).upper()
    log.info('Detail-ref scan: found references=%s; confirmed view labels: %s',
             sorted(refs), list(section_view_positions.keys())[:10])
    missing = []
    for letter in sorted(refs):
        if re.search(rf'\bDETAILS?\s+{letter}\b', confirmed):
            continue
        log.info('DETAIL %s referenced in drawing text but no view label confirmed', letter)
        missing.append({
            'cut_letter':      letter,
            'found_on_view':   f'callout reference "DETAIL {letter}" in drawing',
            'missing_section': f'DETAIL {letter}',
            'bbox':            None,
        })
    return missing


# ── Completeness checks ───────────────────────────────────────────────────────

def _check_required_sections(section_view_positions: dict,
                             profile: DrawingTypeProfile = PPP_PROFILE) -> list:
    """Return presence status for each profile-required section view."""
    all_labels = ' '.join(section_view_positions.keys()).upper()
    result = []
    for name, keywords in profile.required_sections:
        present = any(kw.upper() in all_labels for kw in keywords)
        bbox = None
        if present:
            for label, pos in section_view_positions.items():
                if any(kw.upper() in label for kw in keywords):
                    bbox = pos
                    break
        result.append({'name': name, 'present': present, 'bbox': bbox})
    return result


def _check_notes_completeness(all_text: list,
                              profile: DrawingTypeProfile = PPP_PROFILE) -> list:
    """Return presence status for each required note item via keyword scan of DXF text."""
    full_upper = ' '.join(t['text'] for t in all_text).upper()
    concrete_keys = tuple(f'concrete_{c}' for c in profile.components)
    concrete_found = any(kw in full_upper for kw in profile.concrete_grade_keywords)
    result = []
    for item_key, keywords in profile.note_keywords.items():
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
