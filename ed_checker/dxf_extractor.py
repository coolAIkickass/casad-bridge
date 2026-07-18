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

from .profiles import PPP_PROFILE, DrawingTypeProfile, TRIGGER_WORDS
from .schema import new_drawing_data, diag
from ._memutil import trim_memory

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
        "PIER NO. BOT. OF PILECAP TOP OF PILECAP ..."  — TABLE-1's pier/pile-numbering
            column header row (COMP followed by "NO.")
    Excluded by three guards:
      1. Skip any row where COMP is followed by '=' and a digit.
      2. Require the component keyword to appear in the first two tokens of the row.
      3. Skip any row where COMP is immediately followed by "NO." — that is TABLE-1's
         column header (e.g. "PIER NO."), not a schedule section header. Confirmed on
         real CASAD sheets where this swallowed every bar mark above it as "no component
         found yet" and mis-assigned every bar mark below it to the wrong component.

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
            m = pattern.search(row_text)
            if not m:
                continue
            # Guard 2: component keyword must be within the first two tokens
            # (excludes "MAX. LOAD ON TOP OF PILE", "LOAD ON PILE CAP" etc.)
            if not pattern.search(first_two):
                continue
            # Guard 3: skip "<COMP> NO." style table column headers (TABLE-1's
            # pier/pile-numbering column), not a schedule section header.
            next_tok = row_text[m.end():].split()[:1]
            if next_tok and next_tok[0].rstrip('.').upper() == 'NO':
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
    sv_pos, cut_letters, zone_cut_marks = _extract_section_info(all_text, extents, profile.layout)
    xsec                = _count_cross_section_bars(msp, all_text, schedule, extents, profile, diags, u2mm)
    geometry_from_drawing = _classify_all_dims(msp, all_text, extents, profile, u2mm)
    multileader_callouts, merged_callouts = _extract_multileader_callouts(msp, extents)
    # Programmatic unlabeled-view and missing-detail detection (runs before del msp).
    unlabeled_circles   = _detect_unlabeled_section_circles(
        msp, all_text, extents, profile, u2mm, schedule_bbox=sched_info.get('schedule_bbox'))
    missing_detail_refs = _detect_missing_detail_refs(all_text, sv_pos)
    liner_thk_issues, liner_thickness_mm = _check_liner_thickness_units(msp, extents)
    table1_hdr_issues   = _check_table1_duplicate_headers(all_text, extents, profile.layout)
    unreferenced_views  = _detect_unreferenced_section_views(sv_pos, zone_cut_marks)

    # ezdxf doc and modelspace are done — all data now lives in plain Python dicts.
    # Delete explicitly and gc to break cyclic entity refs (~100-200 MB) before returning.
    # trim_memory() also releases the freed glibc arenas back to the OS — gc.collect()
    # alone leaves RSS at its high-water mark, see _memutil.py.
    del doc, msp, ps_text
    trim_memory()

    # Supplement notes with DIMENSION-derived values when text extraction missed them.
    # DIMENSION entities give exact geometric measurements with no OCR error.
    if dim_data.get('pile_length_mm') and notes.get('pile_length_m') is None:
        notes['pile_length_m'] = round(dim_data['pile_length_mm'] / 1000.0, 3)
        log.info('Notes: pile_length_m set from DIMENSION entity: %.3fm', notes['pile_length_m'])
    if dim_data.get('pile_dia_mm') and notes.get('pile_dia_m') is None:
        notes['pile_dia_m'] = round(dim_data['pile_dia_mm'] / 1000.0, 3)
        log.info('Notes: pile_dia_m set from DIMENSION entity: %.3fm', notes['pile_dia_m'])
    if liner_thickness_mm is not None:
        notes['liner_thickness_mm'] = liner_thickness_mm

    dimension_issues = (
        (dim_data.get('text_override_mismatches') or [])
        + liner_thk_issues
        + table1_hdr_issues
    )
    if dimension_issues:
        log.info('Dimension text/geometry mismatches: %d', len(dimension_issues))

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
        dimension_issues=dimension_issues,
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
        merged_callouts=merged_callouts,
        # DXF-derived unlabeled section circles — merged with vision results in __init__.py
        unlabeled_views=unlabeled_circles,
        # DXF-derived missing DETAIL refs — __init__.py appends cut-letter missing sections
        missing_referenced_sections=missing_detail_refs,
        # Titled SECTION views whose own zone cut-arrow (e.g. "Z1") is missing —
        # the reverse direction of missing_referenced_sections above.
        unreferenced_section_views=unreferenced_views,
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

    \\P (uppercase only) is a STANDALONE paragraph-break code — no ';'-terminated
    parameter. \\L/\\l (underline), \\O/\\o (overline), \\K/\\k (strikethrough) are
    likewise standalone toggles. These must be stripped separately from
    parameterized codes (\\C, \\F, \\A, \\Q, \\H, \\W, \\T, \\S — always followed by
    a short argument then ';') — a single regex treating all codes alike as
    `\\[code][^;]*;` greedily consumes from a standalone code all the way to the
    NEXT unrelated ';'-terminated code anywhere later in the string, confirmed on a
    real drawing where a lone \\P was followed several words later by an unrelated
    \\C4; color code, silently swallowing the entire "CLEAR COVER TO ANY
    REINFORCEMENT" note phrase in between (and every other note in that gap).

    \\p (LOWERCASE) is a different, parameterized code — paragraph *properties*
    (e.g. `\\pxqc;` = centered alignment), always ';'-terminated — it must NOT be
    folded into the \\P handling above; doing so leaves its own parameter fragment
    (e.g. `xqc;`) stuck in the output, confirmed on a real drawing's "TOP OF PIER"
    section-view label (stored as `\\pxqc;{\\W0.9;TOP OF PIER}`) which came out as
    "XQC;TOP OF PIER" instead of "TOP OF PIER" when \\p was wrongly treated as \\P.

    Handle \\P first (→ newline, preserving the line-break structure notes/
    lap-length parsing already depends on), then the remaining standalone toggles
    (deleted, no replacement), then the parameterized codes including \\p.
    """
    text = text.replace('\\P', '\n')
    text = re.sub(r'\\[LlOoKk]', '', text)
    text = re.sub(r'\\[pCcFfAaQqHhWwBbIiTtSs][^;]*;', '', text)
    text = text.replace('{', '').replace('}', '')
    text = text.replace('\\~', ' ')
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


def _zone_anchor_indices(bar_rows: list, col_map: dict, bm: str,
                         x_offset_min: float = 1000.0) -> list[int]:
    """
    Row indices (local to bar_rows) that carry their own TOTAL_LEN cell —
    one per confinement zone.

    TOTAL_LEN is the reliable per-zone anchor, not NOS: on the real
    production schedule, a zone's authoritative '=N' count restatement
    sometimes sits on a row *adjacent* to that zone's own data (e.g. bar
    'y' zone 2's '=240' is one row below its TOTAL_LEN/DIA/UNIT_WT row),
    while TOTAL_LEN always lands exactly once, on the zone's own row,
    with zero redundancy — verified against y/y1/i/i1/j/j1/k/k1/f/f1.
    Returns [] if TOTAL_LEN isn't mapped or no row has a parseable cell
    there — caller treats that as "single zone, no split".
    """
    real_col_map = {k: v for k, v in col_map.items() if not k.endswith('_assigned_x')}
    total_len_x = real_col_map.get('total_length_m')
    if total_len_x is None or not real_col_map:
        return []

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
        total_len_x += x_offset

    x_span = max(real_col_map.values()) - min(real_col_map.values()) if len(real_col_map) > 1 else 100.0
    x_tol = x_span * 0.20

    anchors = []
    for idx, row in enumerate(bar_rows):
        for cell in row:
            if abs(cell['x'] - total_len_x) < x_tol and _safe_float(cell['text']) is not None:
                anchors.append(idx)
                break
    return anchors


def _split_zone_row_ranges(n_rows: int, anchor_indices: list[int]) -> list[tuple[int, int]]:
    """
    Split a bar mark's local row range [0, n_rows) into per-zone (start, end)
    slices, one per TOTAL_LEN anchor row. Boundary sits at the floor-midpoint
    between consecutive anchors — the same convention Pass 2 already uses to
    split between adjacent bar marks above. The first zone absorbs everything
    before its anchor (e.g. the bar-mark label row); the last zone absorbs
    everything after its anchor (e.g. a redundant '=N' restatement on a
    trailing row) — see _aggregate_bar_rows docstring for why that
    restatement doesn't need its own zone.
    """
    if len(anchor_indices) <= 1:
        return [(0, n_rows)]
    ranges = []
    for i, anchor in enumerate(anchor_indices):
        start = 0 if i == 0 else (anchor_indices[i - 1] + anchor) // 2 + 1
        end = n_rows if i == len(anchor_indices) - 1 else (anchor + anchor_indices[i + 1]) // 2 + 1
        ranges.append((start, end))
    return ranges


def _aggregate_bar_rows_for_bar(bar_rows: list, col_map: dict, bm: str,
                                x_offset_min: float = 1000.0) -> dict | list | None:
    """
    Aggregate a bar mark's full row range, splitting into per-confinement-zone
    dicts when more than one TOTAL_LEN anchor is present. Single-zone bars
    (the overwhelming majority) return exactly the same single dict
    _aggregate_bar_rows has always returned — zero behavior change for them.
    """
    anchor_indices = _zone_anchor_indices(bar_rows, col_map, bm, x_offset_min)
    zone_ranges = (_split_zone_row_ranges(len(bar_rows), anchor_indices)
                   if len(anchor_indices) > 1 else [(0, len(bar_rows))])
    zones = []
    for start, end in zone_ranges:
        zone_dict = _aggregate_bar_rows(bar_rows[start:end], col_map, bm, x_offset_min)
        if zone_dict is not None:
            zones.append(zone_dict)
    if not zones:
        return None
    return zones[0] if len(zones) == 1 else zones


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


def _schedule_header_idx(rows: list) -> int | None:
    """Return the index of the first row containing ≥2 of DIA / NOS / LENGTH, or None."""
    for idx, row in enumerate(rows):
        row_text = ' '.join(t['text'] for t in row).upper()
        if sum(1 for k in ('DIA', 'NOS', 'LENGTH') if k in row_text) >= 2:
            return idx
    return None


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

    # Schedule cells are author-typed top-level TEXT. Block-derived text in this region
    # is symbol glyphs and annotation fragments (Ø symbols, attribute numbers) that land
    # in bar row ranges and corrupt column assignment — exclude it.
    all_noblk = [t for t in all_text if not t.get('from_block')]

    # Fast path: assume schedule occupies the right portion of the sheet (common layout).
    sched_x_min = x_min + dw * layout.schedule_x_min_frac
    sched_text = [t for t in all_noblk if t['x'] >= sched_x_min]

    rows = _group_rows(sched_text, tol_frac=layout.sched_row_tol_frac, extents=extents)
    header_idx = _schedule_header_idx(rows)

    if header_idx is None:
        # Fallback: scan the full sheet to locate the header wherever it actually is,
        # then derive the schedule x-region from the header's own position.
        # This handles drawings where the schedule is placed left of the normal threshold
        # (e.g. a pier-only schedule starting at ~15% width instead of the usual 30%).
        full_rows = _group_rows(all_noblk, tol_frac=layout.sched_row_tol_frac, extents=extents)
        hdr_idx_full = _schedule_header_idx(full_rows)
        if hdr_idx_full is None:
            log.warning('Schedule column header row not found')
            diags.append(diag('schedule_header_not_found',
                              'The schedule column header row (DIA / NOS / LENGTH) was not found '
                              'in the DXF. Schedule checks were skipped.'))
            return {}, info

        # Collect all sub-rows within the multi-line header band to find the true
        # leftmost column (MK., SHAPE OF BAR etc. sit a few units above DIA/NOS).
        hdr_y = full_rows[hdr_idx_full][0]['y']
        hdr_band = dh * layout.header_band_frac
        hdr_cells = [c for row in full_rows if abs(row[0]['y'] - hdr_y) <= hdr_band
                     for c in row]
        actual_x_min = min(c['x'] for c in hdr_cells) - 500.0 / u2mm  # 500 mm safety margin
        log.info('Schedule found via full-sheet fallback: header at y=%.0f, '
                 'using x >= %.0f (%.0f%% from left)',
                 hdr_y, actual_x_min, (actual_x_min - x_min) / dw * 100)
        sched_text = [t for t in all_noblk if t['x'] >= actual_x_min]
        rows = _group_rows(sched_text, tol_frac=layout.sched_row_tol_frac, extents=extents)
        header_idx = _schedule_header_idx(rows)
        if header_idx is None:
            # Should not happen since we seeded from the header position, but guard anyway.
            diags.append(diag('schedule_header_not_found',
                              'The schedule column header row (DIA / NOS / LENGTH) was not found '
                              'in the DXF. Schedule checks were skipped.'))
            return {}, info
        diags.append(diag('schedule_x_min_adjusted',
                          f'The schedule was found outside the expected position (x >= '
                          f'{sched_x_min:.0f}, {layout.schedule_x_min_frac*100:.0f}% from left). '
                          f'Extraction re-anchored on the actual header position at '
                          f'{actual_x_min:.0f} ({(actual_x_min-x_min)/dw*100:.0f}% from left).',
                          severity='info'))

    n_block_in_region = sum(1 for t in all_text
                            if t['x'] >= sched_text[0]['x'] and t.get('from_block'))
    if n_block_in_region > 20 and len(sched_text) < n_block_in_region:
        diags.append(diag('schedule_text_mostly_blocks',
                          f'{n_block_in_region} block-derived text entities in the schedule '
                          f'region were excluded vs {len(sched_text)} top-level entities kept. '
                          f'If this schedule is drawn as a block, extraction will be '
                          f'incomplete.', severity='info'))

    if not sched_text:
        log.warning('No text found in schedule area')
        diags.append(diag('schedule_area_empty',
                          'No text found in the schedule area. Schedule checks were skipped.'))
        return {}, info

    log.info('Schedule area: %d text entities', len(sched_text))

    # Bounding box of the schedule's own text cloud — used by
    # _detect_unlabeled_section_circles to exclude bar-shape sketches (e.g. a
    # circular confinement-tie shape in the "Shape of Bar" column) drawn inside the
    # schedule itself. Anchored to the schedule's actual detected extent, not a
    # sheet-wide fraction, so it can't misfire on unrelated geometry elsewhere.
    info['schedule_bbox'] = (
        min(t['x'] for t in sched_text), min(t['y'] for t in sched_text),
        max(t['x'] for t in sched_text), max(t['y'] for t in sched_text),
    )

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

    # Truncate at the first row marking the end of the schedule (TABLE-1's own
    # column header, or the start of a NOTES block). Without this, the LAST bar
    # mark's row-range runs to end-of-data (see Pass 2 below) and — when TABLE-1
    # or NOTES is stacked directly below the schedule in the same x-region, as on
    # some CASAD sheets — the zone-row aggregator vacuums up TABLE-1/NOTES text as
    # bogus zone-continuation data for that bar mark. A no-op when TABLE-1/NOTES
    # sit outside the schedule's x-region, which is the more common layout.
    _SCHEDULE_END_RE = re.compile(r'\bTABLE-1\b|\bTABLE\s+1\b|\bNOTES\s*:', re.IGNORECASE)
    for _end_idx, _row in enumerate(data_rows):
        if _SCHEDULE_END_RE.search(' '.join(t['text'] for t in _row)):
            data_rows = data_rows[:_end_idx]
            break
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
        anchor_indices = _zone_anchor_indices(bar_rows, col_map, bm, x_offset_min)
        zone_ranges = (_split_zone_row_ranges(len(bar_rows), anchor_indices)
                       if len(anchor_indices) > 1 else [(0, len(bar_rows))])

        # Aggregate per zone (usually just one) so a multi-row confinement bar
        # (e.g. y/y1) yields a list of per-zone dicts instead of one pre-summed
        # dict — preserves the row-level detail the comparator needs to match
        # zone rows against design Excel rows individually or sum-and-compare.
        zone_dicts = []
        for z_start, z_end in zone_ranges:
            zone_rows = bar_rows[z_start:z_end]
            zone_dict = _aggregate_bar_rows(zone_rows, col_map, bm, x_offset_min)
            if zone_dict is None:
                continue
            # Attach exact DXF bbox for this zone's own rows so the review UI can
            # highlight the precise schedule row(s) instead of the whole bar's range.
            if zone_rows:
                all_ys = [cell['y'] for row in zone_rows for cell in row]
                all_xs = [cell['x'] for row in zone_rows for cell in row]
                if all_ys and all_xs:
                    row_pad = dh * layout.sched_row_tol_frac
                    zone_dict['row_bbox'] = _to_bbox(
                        min(all_xs) - x_span * 0.05,
                        min(all_ys) - row_pad,
                        max(all_xs) + x_span * 0.05,
                        max(all_ys) + row_pad,
                        extents,
                    )
            zone_dict['bar_mark'] = bm
            zone_dicts.append(zone_dict)

        if not zone_dicts:
            continue
        bar_data = zone_dicts[0] if len(zone_dicts) == 1 else zone_dicts
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

    # Pass 3: paperspace fallback — CASAD title blocks live entirely in a paperspace
    # layout, not modelspace, so this always runs when paperspace text exists (not
    # gated on how many fields Pass 1/2 already filled — modelspace can coincidentally
    # fill a few unrelated fields, e.g. a section's own SCALE attrib, which used to
    # block this pass from ever running even though the drawing number/date/revision
    # only exist in paperspace). `_title_block_pattern_pass` only fills still-unset
    # fields, so re-running it here is safe/idempotent.
    #
    # No bottom-right-quadrant filter here (unlike Pass 2's modelspace scan): a
    # paperspace layout in these DXFs contains only the plot border/title-block/
    # revision-table content already — nothing else to filter out. The quadrant
    # box previously applied here assumed title-block content clusters in the
    # rightmost 40%, but CASAD's revision-date table sits at the *left* edge of an
    # otherwise full-width title block, so that box silently dropped the date.
    if ps_text:
        before = sum(1 for v in result.values() if v)
        _title_block_pattern_pass(ps_text, result)
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



# Drawing-number formats seen in practice: slash-delimited (ABC/XYZ/...) and CASAD's
# own hyphen-delimited convention (e.g. "PGII-MJB-96+814-002" — project-contract-
# chainage-sheet). '+' allowed since chainage segments use it (96+814).
_DRG_NO_PATTERNS = (
    r'[A-Z]{2,}/[A-Z]{2,}/',
    r'[A-Z]{2,}-[A-Z0-9]{2,}-[\dA-Z+]{2,}-\d{2,}',
)


def _title_block_pattern_pass(tb_text: list, result: dict):
    """Run the title-block field regexes over a text list, filling unset fields in place."""
    for t in tb_text:
        s = t['text'].strip()
        su = s.upper()

        if not result.get('revision') and re.match(r'^R\d+$', s, re.IGNORECASE):
            result['revision'] = s.upper()

        # Date: dd/mm/yyyy or dd/mm/yy — CASAD dates use a 2-digit year ("29/03/23").
        if not result.get('date') and re.match(r'\d{2}[/-]\d{2}[/-]\d{2,4}', s):
            result['date'] = s

        if not result.get('scale') and re.search(r'AS SHOWN|NTS|\b1\s*:\s*\d+\b', su):
            result['scale'] = s

        if not result.get('drawing_number') and any(re.search(p, s) for p in _DRG_NO_PATTERNS):
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

        # Names with initials: "A.B.NAME". Skip text nested inside a block reference
        # (from_block=True) — CASAD's title block uses a company-wide signature-stamp
        # block that lists every engineer's name as static geometry, with only one
        # actually visible per row via an AutoCAD dynamic-block visibility state that
        # ezdxf cannot resolve. Reading raw block content here would silently grab
        # an arbitrary (often wrong) name instead of the one actually shown on this
        # drawing. Only a top-level, author-placed name (not part of that block) is
        # trustworthy enough to use.
        if not t.get('from_block') and re.match(r'^[A-Z]\.[A-Z]\.\w+$', s):
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
        comps_in = [c for c in comps_longest
                    if re.search(r'\b' + re.escape(c.upper()) + r'\b', line)]
        if not comps_in:
            continue
        if len(comps_in) == 1:
            comp = comps_in[0]
            expected = (notes.get(f'concrete_{comp}') or '').upper()
            if not expected:
                continue
        else:
            # Combined section (e.g. "FOR PILECAP & PIER"): only check when all
            # named components agree on grade — ambiguous if they differ.
            grades = {(notes.get(f'concrete_{c}') or '').upper() for c in comps_in}
            grades.discard('')
            if len(grades) != 1:
                continue
            expected = grades.pop()
            comp = ' & '.join(comps_in)

        y_label = row[0]['y']         # DXF Y (bottom-up)
        # Search ±view_h around the label: section views may be above or below their
        # title label depending on drafter convention.
        y_low  = y_label - view_h
        y_high = y_label + view_h

        for t in all_text:
            if not (y_low <= t['y'] <= y_high):
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


def _layer_excluded(layer: str) -> bool:
    layer = layer.upper()
    return any(kw in layer for kw in _EXCLUDED_LAYER_KEYWORDS)

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
                               view_labels: list | None = None,
                               profile: DrawingTypeProfile = PPP_PROFILE) -> dict:
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

    After the polyline/arc pass above, also runs a rebar-dot-ring fallback
    (_add_dot_ring_regions) for components whose true outline isn't one closed
    polyline our bbox+aspect heuristic can bound — e.g. a capsule-shaped pier
    drawn as several curve fragments, no single one of which is pier-sized on
    its own. See that function's docstring for how it avoids ever overriding a
    polyline-based detection.
    """
    x_min, y_min, x_max, y_max = extents
    dw = (x_max - x_min) or 1.0
    dh = (y_max - y_min) or 1.0
    regions: dict = {}

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
            # Sheet border / extents rectangle — exact match to whole-sheet size.
            if w > dw * 0.95 and h > dh * 0.95:
                continue

            w_mm, h_mm = w * u2mm, h * u2mm
            bbox = (min(xs), min(ys), max(xs), max(ys))
            cx = (min(xs) + max(xs)) / 2
            cy = (min(ys) + max(ys)) / 2
            aspect = w / h if h > 0 else 0

            # Layer exclusion only applies to candidates NOT already meter-scale
            # pilecap-sized — CASAD sometimes draws the concrete outline of a
            # pilecap itself on a layer named e.g. "S_REINF" (confirmed on a real
            # capsule-pier sheet), and a blanket layer exclusion would throw away
            # the only closed polyline marking that component's boundary, breaking
            # Tier-2 dimension routing for it entirely.
            # Deliberately NOT extended to the pier-sized bypass below: a real
            # stirrup bent-bar shape sketch (confirmed on the same sheet, layer
            # "S_REINF STIRRUPS", ~315×503mm) sits *inside* the pier's 300–6000mm /
            # 0.6–1.6-aspect bounds and would leak through as a spurious pier
            # region if the bypass applied there too. The pilecap bypass is safe
            # because it additionally requires aspect > 1.5 and width ≥1000mm —
            # far outside any single bar's bend-shape footprint.
            layer = str(poly.dxf.get('layer', ''))
            is_pilecap_sized = (aspect > 1.5 and _PILECAP_MIN_W_MM <= w_mm <= _PILECAP_MAX_W_MM
                                and h_mm >= _PILECAP_MIN_H_MM)
            if not is_pilecap_sized and _layer_excluded(layer):
                continue

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

    if view_labels:
        _add_dot_ring_regions(msp, regions, view_labels, profile, u2mm)

    log.info('Component regions detected: %s', {k: v['type'] for k, v in regions.items()})
    return regions


_DOT_RING_MIN_COUNT = 8   # mirrors _count_cross_section_bars' "inserted often enough to be
                          # a per-bar symbol" fallback threshold


def _add_dot_ring_regions(msp, regions: dict, view_labels: list,
                           profile: DrawingTypeProfile, u2mm: float) -> None:
    """
    Fallback component-region detection via rebar-dot ring bounding box, for
    components whose true outline isn't one closed polyline the boundary-based
    pass above can bound. Confirmed on a real capsule-shaped pier sheet: the
    pier's oval outline is drawn as several small curve fragments (the largest
    single fragment found was ~500×1800mm — nowhere near pier-sized on its
    own), so the polyline pass finds nothing there at all, even though the
    component clearly exists and is clearly sized correctly once you look at
    where its 50 individual "REIN.DOT" rebar markers actually sit (bounding
    box ≈2135×1640mm — matching the design pier's 2300×1800mm to within
    concrete cover). CASAD places one such dot block per bar around a
    section's perimeter, so that ring's own bounding box is a reliable stand-in
    for the component's true footprint.

    Anchored to 'section'-type view labels that name exactly one component
    (e.g. "CROSS SECTION OF PIER") so this can't be spuriously triggered by an
    unrelated dot cluster elsewhere on the sheet, and skips any label whose
    component already has a polyline-based region nearby — this fallback must
    never override a more precise detection, only fill a gap.
    """
    section_labels = [vl for vl in view_labels
                       if vl['view_type'] == 'section' and len(vl['components']) == 1]
    if not section_labels:
        return

    insert_points = _collect_dot_insert_points(msp)
    name_res = [re.compile(p, re.IGNORECASE) for p in profile.dot_block_patterns]
    named_dots = {n: pts for n, pts in insert_points.items()
                  if any(rx.search(n) for rx in name_res)}
    dot_blocks = named_dots or {n: pts for n, pts in insert_points.items()
                                 if len(pts) >= _DOT_RING_MIN_COUNT}
    all_dots = [p for pts in dot_blocks.values() for p in pts]
    if not all_dots:
        return

    # Candidate pool per label uses the generous _MAX_LABEL_DIST_MM radius (same
    # constant the polyline pass above uses for label proximity), but that radius
    # alone is NOT the ring boundary — on a sheet with several piers shown close
    # together, dots from two different piers can both land inside one label's
    # radius. _largest_cluster's gap-based flood fill (the same clustering
    # _count_cross_section_bars already uses to separate one section's dots from
    # its neighbours) finds the actual coherent ring; picking the cluster nearest
    # the label picks the right ring when more than one exists in range.
    search_r = _MAX_LABEL_DIST_MM / u2mm if u2mm else _MAX_LABEL_DIST_MM
    cluster_gap = _XSEC_CLUSTER_GAP_MM / u2mm if u2mm else _XSEC_CLUSTER_GAP_MM
    next_idx: dict = {}
    for vl in section_labels:
        comp = next(iter(vl['components']))
        lx, ly = vl['x'], vl['y']

        already = any(
            r['type'] == comp and
            ((r['bbox'][0] + r['bbox'][2]) / 2 - lx) ** 2 +
            ((r['bbox'][1] + r['bbox'][3]) / 2 - ly) ** 2 < search_r ** 2
            for r in regions.values()
        )
        if already:
            continue

        candidates = [p for p in all_dots if (p['x'] - lx) ** 2 + (p['y'] - ly) ** 2 < search_r ** 2]
        if len(candidates) < _DOT_RING_MIN_COUNT:
            continue

        clusters = _all_clusters(candidates, max_gap=cluster_gap)
        clusters = [c for c in clusters if len(c) >= _DOT_RING_MIN_COUNT]
        if not clusters:
            continue

        def _cluster_dist(c):
            ccx = sum(p['x'] for p in c) / len(c)
            ccy = sum(p['y'] for p in c) / len(c)
            return (ccx - lx) ** 2 + (ccy - ly) ** 2
        nearby = min(clusters, key=_cluster_dist)

        xs = [p['x'] for p in nearby]
        ys = [p['y'] for p in nearby]
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        if w <= 0 or h <= 0:
            continue
        w_mm, h_mm = w * u2mm, h * u2mm
        aspect = w / h

        is_pilecap_sized = (comp == 'pilecap' and aspect > 1.5
                            and _PILECAP_MIN_W_MM <= w_mm <= _PILECAP_MAX_W_MM
                            and h_mm >= _PILECAP_MIN_H_MM)
        is_pier_sized = (comp == 'pier' and 0.6 <= aspect <= 1.6
                         and _PIER_MIN_W_MM <= w_mm <= _PIER_MAX_W_MM
                         and _PIER_MIN_H_MM <= h_mm <= _PIER_MAX_H_MM)
        if not (is_pilecap_sized or is_pier_sized):
            continue

        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        idx = next_idx.get(comp, 0)
        regions[f'{comp}_dotring_{idx}'] = {
            'type': comp, 'bbox': (min(xs), min(ys), max(xs), max(ys)),
            'center': (cx, cy), 'width': w, 'height': h,
        }
        next_idx[comp] = idx + 1


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
    regions = _detect_component_regions(msp, extents, u2mm, view_labels, profile)
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


_MERGED_CALLOUT_RE = re.compile(r'^([a-z]\d{0,2})[/\s]+([a-z]\d{0,2})$')


def _extract_multileader_callouts(msp, extents: tuple) -> tuple:
    """
    Extract bar mark annotations from MULTILEADER entities.
    Text is in the MLeader context MTEXT, via entity.context.mtext.default_content.
    Returns (callouts, merged_callouts) where:
      callouts        = [{bar_mark, x_pct, y_pct}]   — single valid bar marks
      merged_callouts = [{text, x_pct, y_pct}]        — merged "y/y1"-style callouts
        that should be two separate leaders, not one combined annotation.
    """
    callouts = []
    merged_callouts = []
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
            pos = ctx.mtext.insert
            x_pct, y_pct = _pos_to_pct(float(pos[0]), float(pos[1]), extents)
            if re.match(r'^[a-z]\d{0,2}$', bar_mark):
                callouts.append({'bar_mark': bar_mark, 'x_pct': x_pct, 'y_pct': y_pct})
            elif _MERGED_CALLOUT_RE.match(bar_mark):
                # Two bar marks fused into one callout (e.g. "y/y1") — each zone
                # should have its own separate leader so the checker can verify them
                # independently; flag for the drafter to split into two leaders.
                merged_callouts.append({'text': bar_mark, 'x_pct': x_pct, 'y_pct': y_pct})
        except Exception as ex:
            log.debug('MULTILEADER parse error: %s', ex)
    log.info('MULTILEADER callouts: %d extracted, %d merged', len(callouts), len(merged_callouts))
    return callouts, merged_callouts


_TABLE1_LABEL_RE = re.compile(r'\bTABLE[-\s]?1\b', re.IGNORECASE)
_TABLE1_DATA_ROW_RE = re.compile(r'^\(?(LP|RP)\d+\b', re.IGNORECASE)
_TABLE1_HEADER_WORD_RE = re.compile(
    r'\b(TOP|BOT|BOTTOM|GROUND|LVL|PIERCAP|PILECAP|PIER)\b', re.IGNORECASE)


def _check_table1_duplicate_headers(all_text: list, extents: tuple,
                                     layout: LayoutConfig) -> list:
    """
    Flag a TABLE-1 (pier/pile levels table) whose header row has two columns with
    identical text — a drafter copy-pasted a header cell and forgot to update it
    (e.g. two columns both labelled "TOP OF PIER" when one should read "TOP OF
    PILECAP"). TABLE-1's numeric cell VALUES are deliberately not data-checked
    elsewhere (CASAD usually pastes the table as an embedded OLE Excel object that
    ezdxf can't read), but the header row is frequently real TEXT/MTEXT even on
    those sheets, and a duplicated header string is unambiguous regardless of
    whether the values beneath it can be verified — this check needs no design
    input and produces nothing when the header row isn't real text (the OLE case).
    Block-derived text is excluded — same reason schedule parsing excludes it.

    Handles multiple TABLE-1 instances on one sheet (e.g. a combined multi-pier
    drawing with a separate pier/pile-level table per pier) — each instance is
    checked independently. Confirmed necessary on a real production sheet
    (`12_DETAILS OF PILECAP & PIER_P27B_R0-PPCP.dxf`): two side-by-side TABLE-1
    instances each have entirely distinct headers, but scanning by Y-position
    alone (ignoring which table a cell's X position belongs to) pooled both
    tables' header cells together and reported every header as "duplicated"
    even though neither table individually repeats a header. Labels are sorted
    by X and the midpoint between adjacent labels forms the x-window boundary
    for each — safe as long as side-by-side tables don't overlap in X, which
    they don't by construction (they'd visually collide on the sheet otherwise).
    A single-TABLE-1 sheet (the common case) gets one window spanning the full
    sheet width, identical to the prior behaviour.

    Returns [{description, bbox}] suitable for drawing_data['dimension_issues'].
    """
    noblk = [t for t in all_text if not t.get('from_block')]
    label_cells = [t for t in noblk if _TABLE1_LABEL_RE.search(t['text'])]
    if not label_cells:
        return []

    x_min, y_min, x_max, y_max = extents
    labels_by_x = sorted(label_cells, key=lambda t: t['x'])
    windows = []
    for i, label in enumerate(labels_by_x):
        left = x_min if i == 0 else (labels_by_x[i - 1]['x'] + label['x']) / 2
        right = x_max if i == len(labels_by_x) - 1 else (label['x'] + labels_by_x[i + 1]['x']) / 2
        windows.append((label, left, right))

    rows = _group_rows(noblk, tol_frac=layout.sched_row_tol_frac, extents=extents)

    issues = []
    for label, left, right in windows:
        label_y = label['y']
        header_cells = []
        for row in rows:
            row_y = row[0]['y']
            if row_y >= label_y - 1e-6:
                continue  # at/above this TABLE-1 label itself
            windowed = [c for c in row if left <= c['x'] < right]
            if not windowed:
                continue
            windowed_sorted = sorted(windowed, key=lambda c: c['x'])
            row_text = ' '.join(c['text'] for c in windowed_sorted).strip()
            if _TABLE1_DATA_ROW_RE.match(row_text):
                break  # reached this table's first data row (e.g. "LP4 110.151 ...")
            header_cells.extend(windowed_sorted)
            if len(header_cells) > 40:  # safety cap
                break

        seen: dict = {}
        for c in header_cells:
            txt = c['text'].strip()
            if not _TABLE1_HEADER_WORD_RE.search(txt):
                continue  # only compare cells that look like real column-header phrases
            key = re.sub(r'\s+', ' ', txt).upper()
            seen.setdefault(key, []).append(c)

        for key, cells in seen.items():
            if len(cells) < 2:
                continue
            cx = sum(c['x'] for c in cells) / len(cells)
            cy = sum(c['y'] for c in cells) / len(cells)
            x_pct, y_pct = _pos_to_pct(cx, cy, extents)
            issues.append({
                'description': (
                    f'TABLE-1 has {len(cells)} columns both labelled "{key}" — a header '
                    f'was likely copy-pasted without updating it. Verify each column '
                    f'heading is correct and distinct.'
                ),
                'bbox': {'x': x_pct, 'y': y_pct, 'width': 10.0, 'height': 3.0},
            })
    return issues


_LINER_THK_RE = re.compile(r'(\d+(?:\.\d+)?)\s*\bM\b\s*THK')
_LINER_THK_MM_RE = re.compile(r'(\d+(?:\.\d+)?)\s*MM\s*THK', re.IGNORECASE)


def _check_liner_thickness_units(msp, extents: tuple) -> tuple:
    """
    Scoped to MULTILEADER callouts that mention "LINER" so a legitimate "M"/
    "MM" used elsewhere in dimension text is never touched. Does exactly one
    thing: detect a units typo — thickness written in metres ("6 M THK.")
    instead of millimetres ("6 MM THK."). A steel casing liner is physically
    a few millimetres thick — a bare "M" here is a dropped letter, not a
    genuine multi-metre design. `\\bM\\b` requires "M" as its own token —
    "MM THK." (the correct form) has no word boundary between the two M's
    and is never matched by this pattern.

    The below-code-minimum-thickness check (IRC:78 Cl. 709.1.4, 6mm floor)
    used to live here too but has moved to the knowledge_rules rule engine
    (rules/irc78_pile_foundation.yaml, rule IRC78-709.1.4-LINER-MIN-THICKNESS)
    — this function's second return value exposes the parsed correctly-
    formatted MM value so the caller can populate notes['liner_thickness_mm']
    for that rule to evaluate generically, instead of this function deciding
    pass/fail itself.

    Returns ([{description, bbox}] suitable for drawing_data['dimension_issues'],
    liner_thickness_mm: float | None — the last correctly-formatted "N MM THK."
    value found, or None if no LINER callout used correct MM units).
    """
    issues = []
    liner_thickness_mm = None
    for e in msp.query('MULTILEADER'):
        try:
            ctx = e.context
            if not ctx or not ctx.mtext:
                continue
            raw = ctx.mtext.default_content or ''
        except Exception:
            continue
        text = _strip_dim_override(raw)
        if 'LINER' not in text.upper():
            continue

        try:
            pos = ctx.mtext.insert
            x_pct, y_pct = _pos_to_pct(float(pos[0]), float(pos[1]), extents)
        except Exception:
            x_pct, y_pct = 50.0, 50.0
        bbox = {'x': x_pct, 'y': y_pct, 'width': 8.0, 'height': 3.0}

        m = _LINER_THK_RE.search(text.upper())
        if m:
            issues.append({
                'description': (
                    f'The liner thickness callout reads "{m.group(1)} M THK." '
                    f'({m.group(1)} metres) — this is almost certainly a missing "M" and '
                    f'should read "{m.group(1)} MM THK." (millimetres). A steel casing '
                    f'liner {m.group(1)} metres thick is not physically plausible.'
                ),
                'bbox': bbox,
            })
            continue

        m_mm = _LINER_THK_MM_RE.search(text)
        if m_mm:
            liner_thickness_mm = float(m_mm.group(1))
    return issues, liner_thickness_mm


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
        'text_override_mismatches': [],
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
                # Pattern 0: bare-numeric override that disagrees with the entity's own
                # computed measurement — the drafter manually typed/edited the displayed
                # dimension text and it no longer matches the drawn geometry (e.g. a digit
                # typo, or a stale override left over from copy-pasted geometry). A pure
                # DXF self-consistency check — needs no design-input comparison, so it
                # also runs when no design Excel was uploaded. Only bare numbers are
                # checked (no letters/backslash codes) to avoid false positives on
                # legitimate overrides that intentionally differ from the raw
                # defpoint-to-defpoint distance: "LENGTH OF PILE = 18000" (compressed
                # break-line dimension), "3000\Xy" (DETAIL zone label), "840 DIA"
                # (diameter callout), "i1+j1+k1\X2400" (bar-mark-sum label).
                #
                # Relative threshold is 25%, not a tight tolerance — CASAD's own
                # standing drawing note states "THIS DRAWING IS NOT TO SCALE & WRITTEN
                # DIMENSION SHALL BE FOLLOWED", i.e. the override text is the
                # authoritative design value by convention and the underlying geometry
                # is explicitly permitted to be schematic rather than exact. Confirmed
                # on a real production "Without Errors" sheet
                # (`12_DETAILS OF PILECAP & PIER_P27B_R0-PPCP.dxf`) with legitimate
                # override/geometry gaps up to ~20% (round-pier radial dimensions,
                # inherently imprecise chord geometry) that must NOT be flagged. The
                # two confirmed seeded errors in the training set (1300 vs 2300mm,
                # 200 vs 150mm) sit at 43% and 33% respectively — comfortably clear of
                # this floor on the other side.
                if re.match(r'^-?\d+(\.\d+)?$', override.strip()):
                    override_val = float(override.strip())
                    if abs(override_val - val) > max(2.0, val * 0.25):
                        margin = 400.0 / u2mm
                        bbox = _to_bbox(tx - margin, ty + margin, tx + margin, ty - margin, extents)
                        result['text_override_mismatches'].append({
                            'description': (
                                f'A dimension is labelled "{override.strip()}" but the actual '
                                f'distance measured from the drawing geometry is {val:.0f}mm — '
                                f'the displayed value was manually overridden and no longer '
                                f'matches the drawn geometry. Verify which is correct and fix '
                                f'the mismatch.'
                            ),
                            'bbox': bbox,
                        })
                    continue

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


# ── View label extraction (for Tier 2 plan/section disambiguation) ────────────

_VIEW_LABEL_RE = re.compile(
    r'^(CROSS\s+SECTION|SECTION|PLAN OF|REINFORCEMENT PLAN OF|DETAIL)\b', re.IGNORECASE)


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
        elif upper.startswith('SECTION') or upper.startswith('CROSS SECTION'):
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
    Return (section_view_positions, cut_letters, zone_cut_marks).
    section_view_positions: {label_text: {x,y,w,h}} in PDF-style percentages.
    cut_letters: set of single uppercase letters appearing ≥2 times in left portion.
    zone_cut_marks: set of compound letter+digit cut marks (e.g. "Z1", "Z2")
        appearing ≥2 times — the same convention as cut_letters, but for a pile/
        component with more than one confinement/remaining-length zone, where
        each zone gets its own cut-arrow (e.g. "SECTION Z1-Z1" alongside plain
        "SECTION Z-Z"). Consumed by _detect_unreferenced_section_views for the
        reverse direction of the existing cut-letter cross-check: a titled
        SECTION view whose own cut-arrow doesn't actually appear anywhere.
    """
    layout = layout or PPP_PROFILE.layout
    x_min, y_min, x_max, y_max = extents
    dw = x_max - x_min
    dh = y_max - y_min

    all_rows = _group_rows(all_text, tol_frac=layout.row_tol_frac, extents=extents)

    section_view_positions = {}
    single_letter_counts = {}
    single_letter_ys     = {}  # letter → list of y-values, for axis-label filter
    zone_mark_counts = {}
    zone_mark_ys     = {}      # same idea as single_letter_ys, for compound marks (e.g. "Z1")

    # Cut-letter detection is NOT gated by views_x_max_frac (removed 2026-06-24,
    # found on ALL_SECTIONS-02.dxf): that fraction assumes cut marks always sit in
    # a fixed left band, but on a full/combined sheet a cut mark can legitimately
    # sit anywhere (this file's "A"/"B" cut marks on the PLAN OF PILECAP elevation
    # were past the 55% mark and were silently dropped from cut_letters entirely —
    # same root cause as the SECTION-label gating bug in _count_cross_section_bars).
    # Safe to drop: bar marks are lowercase (a, b, c... per convention) so they
    # never match the s.isupper() filter below, and the y-band check a few lines
    # down already excludes repeated uppercase pier/grid-column labels — the two
    # things the position gate was guarding against don't actually need it.
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

        # Block-derived text is excluded EXCEPT ATTRIBs — ATTRIBs are user-typed
        # tag values on INSERT blocks (e.g. the "C"/"D" letter on a cut-mark arrow
        # block), not glyph substitutions from symbol block geometry.
        for t in row:
            if t.get('from_block') and not t.get('is_attrib'):
                continue
            s = t['text'].strip()
            if len(s) == 1 and s.isupper() and s.isalpha():
                single_letter_counts[s] = single_letter_counts.get(s, 0) + 1
                single_letter_ys.setdefault(s, []).append(t['y'])
            elif re.match(r'^[A-Z]\d{1,2}$', s):
                # Compound zone cut-mark (e.g. "Z1", "Z2") — a pile with more than
                # one confinement/remaining-length zone gets its own cut-arrow per
                # zone, distinct from the plain single-letter cut marks above.
                # Uppercase-only match excludes lowercase bar-mark callouts (e.g. "z1").
                zone_mark_counts[s] = zone_mark_counts.get(s, 0) + 1
                zone_mark_ys.setdefault(s, []).append(t['y'])

    # No second raw-entity pass: _group_rows() partitions all_text completely (every
    # item lands in exactly one row), so the loop above already visits each text item
    # once. A second pass over all_text used to double-count every qualifying letter —
    # found 2026-06-24 via ALL_SECTIONS-02.dxf, where cut-mark pairs sharing one y
    # (e.g. "A" ... "A" either side of one cut line) got double-counted from 2 to 4
    # occurrences, which spuriously tripped the "≥3 in one y-band = axis label" filter
    # below and silently dropped genuine cut letters ("A", "Z") from the result.

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

    zone_cut_marks = set()
    for mark, count in zone_mark_counts.items():
        if count < 2:
            continue
        ys = zone_mark_ys.get(mark, [])
        if count >= 3 and (max(ys) - min(ys)) <= _y_band:
            continue  # axis label, not a cut mark
        zone_cut_marks.add(mark)

    log.info('Section info: %d labels, cut_letters=%s, zone_cut_marks=%s',
             len(section_view_positions), sorted(cut_letters), sorted(zone_cut_marks))
    return section_view_positions, cut_letters, zone_cut_marks


_SECTION_TITLE_MARK_RE = re.compile(r'SECTION\s+([A-Z]\d{1,2})-\1')


def _detect_unreferenced_section_views(section_view_positions: dict,
                                        zone_cut_marks: set) -> list:
    """
    Reverse direction of the existing cut-letter cross-check: instead of "a cut
    mark exists but its section view is missing", catch "a titled section view
    exists (e.g. 'SECTION Z1-Z1') but its own originating cut-arrow doesn't
    appear anywhere in the drawing" — the reader has no way to tell where that
    cut is actually taken from.

    Scoped to compound zone marks only (letter+digit, e.g. "Z1", "Z2") — a pile
    or component with more than one confinement/remaining-length zone draws a
    separate cut-arrow per zone in its DETAIL view. Deliberately NOT extended to
    plain single-letter sections (e.g. "SECTION A-A") yet: those cut marks are
    sometimes drawn by conventions this scan doesn't recognise, and a false
    "missing" flag on a routine A-A/B-B section would be a much noisier mistake
    than under-flagging here — same conservative bias as the existing cut-mark
    inference code in this module.

    Returns [{missing_mark, view_label, bbox}] suitable for
    drawing_data['unreferenced_section_views'].
    """
    if not section_view_positions:
        return []
    missing = []
    for label, bbox in section_view_positions.items():
        m = _SECTION_TITLE_MARK_RE.search(label.upper())
        if not m:
            continue
        mark = m.group(1)
        if mark in zone_cut_marks:
            continue
        missing.append({
            'missing_mark': mark,
            'view_label':   label,
            'bbox':         bbox,
        })
    return missing


# ── Cross-section bar counting ────────────────────────────────────────────────

# Absolute mm, NOT sheet-relative fractions (see profiles.py LayoutConfig comment).
# A fraction of sheet width/height gives a reasonable box on a small cropped
# single-section DXF but balloons to tens of metres on a full multi-view sheet,
# sweeping a neighbouring section view's dots into the same cluster. Found 2026-06-22:
# SECTION C-C and SECTION D-D labels sit only ~4.5m apart on the real P3-P7 sheet, but
# the old xsec_dx_frac=0.25 gave a 26.7m half-width search box (sheet width 107m) —
# every dot on roughly half the sheet (359 of ~436) landed in "one" section's cluster.
_XSEC_REGION_MARGIN_MM   = 600.0    # margin around a detected component's own bbox
_XSEC_FALLBACK_DX_MM     = 4500.0   # used only when no matching region was detected
_XSEC_FALLBACK_BELOW_MM  = 5000.0
_XSEC_FALLBACK_ABOVE_MM  = 700.0
_XSEC_CLUSTER_GAP_MM     = 2600.0   # max gap between dots of one section view cluster
_XSEC_DOT_MAX_R_MM       = 600.0    # max bar-dot radius (filters section boundary circles)

# Chord-distance ratio thresholds for _compute_spacing_issues (ratio to the ring's
# own median chord, not to a unit-circle angular gap — see that function's docstring
# for why angular gap doesn't generalize across rectangle aspect ratios).
_XSEC_GAP_RATIO          = 1.3      # chord > this × median ⇒ flag missing/oversized gap
_XSEC_CLUSTER_RATIO      = 0.4      # chord < this × median ⇒ flag overly tight bars


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

    # Find section labels anywhere on the sheet. NOT gated by views_x_max_frac —
    # that fraction assumes the schedule sits in a fixed right-hand band, which broke
    # on a combined/full sheet where SECTION Z-Z, C-C, D-D sit past the 55% mark
    # (found 2026-06-24 on ALL_SECTIONS-02.dxf: those three section labels were
    # silently skipped before the regex even ran, so their bar counts — including
    # Z-Z's genuine bundle-bar inner ring — were never verified). The "SECTION X-X"
    # regex below is specific enough on its own; a stray match in a schedule/notes
    # region would still need a nearby component region and ≥4 dot hits to produce
    # a result, so dropping the position gate does not reintroduce false positives.
    results = []

    # Bare labels (e.g. "SECTION C-C" cut from a multi-component parent elevation like
    # "SECTION A-A FOR PILECAP & PIER") name no component of their own — resolve via
    # cut-mark geometry before falling back to skipping the count. See docstring.
    view_labels = _extract_view_labels(all_text, profile)
    regions = _detect_component_regions(msp, extents, u2mm, view_labels, profile)
    cutmark_components = _infer_cutmark_components(all_text, view_labels, regions, profile)

    for t in all_text:
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

        # A label naming more than one component (e.g. "SECTION A-A FOR PILECAP &
        # PIER") draws bars from multiple bar marks in the same view — there is no
        # way to attribute an individual dot to a specific bar mark from geometry
        # alone, so counting them all under whichever mark _find_bar_mark_for_section
        # happens to pick produces a meaningless total (e.g. pilecap 'a' bars plus
        # the 'e' stirrup dots plus the pier cage, all reported as bar 'a').
        # comps_longest_first + strip-as-matched avoids double-counting e.g. 'PILE'
        # as a second component when it's really just a substring of 'PILECAP'.
        _label_remaining = label_text
        named_comps = []
        for c in profile.comps_longest_first():
            if c.upper() in _label_remaining:
                named_comps.append(c)
                _label_remaining = _label_remaining.replace(c.upper(), '')
        if len(named_comps) > 1:
            diags.append(diag('xsec_multi_component_label',
                              f'Section "{label_text}": label names multiple components '
                              f'({", ".join(named_comps)}) — bar dots from all of them '
                              f'would be indistinguishable, so the count was not verified. '
                              f'Check this view manually.', severity='info'))
            continue

        bar_mark = _find_bar_mark_for_section(comp, schedule)

        # Search for bar dots in a region around the section label. Anchored to the
        # section's own detected component region when available — its bbox already
        # excludes neighbouring views by construction, unlike a fixed offset from the
        # label (see _XSEC_* constants' docstring). Falls back to a label-relative box
        # only when region detection found nothing for this component (e.g. an
        # elevation view with no closed-polyline/arc geometry to detect).
        margin = _XSEC_REGION_MARGIN_MM / u2mm if u2mm else _XSEC_REGION_MARGIN_MM
        comp_regions = [r for r in regions.values() if r['type'] == comp]
        if comp_regions:
            nearest = min(comp_regions, key=lambda r: (
                (r['bbox'][0] + r['bbox'][2]) / 2 - lx) ** 2 +
                ((r['bbox'][1] + r['bbox'][3]) / 2 - ly) ** 2)
            rx0, ry0, rx1, ry1 = nearest['bbox']
            search_x0, search_x1 = rx0 - margin, rx1 + margin
            search_y0, search_y1 = ry0 - margin, ry1 + margin
        else:
            fb_dx    = _XSEC_FALLBACK_DX_MM / u2mm if u2mm else _XSEC_FALLBACK_DX_MM
            fb_below = _XSEC_FALLBACK_BELOW_MM / u2mm if u2mm else _XSEC_FALLBACK_BELOW_MM
            fb_above = _XSEC_FALLBACK_ABOVE_MM / u2mm if u2mm else _XSEC_FALLBACK_ABOVE_MM
            search_x0 = lx - fb_dx
            search_x1 = lx + fb_dx
            search_y0 = ly - fb_below   # below label (DXF y decreases downward)
            search_y1 = ly + fb_above  # slightly above label

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
            max_bar_r = _XSEC_DOT_MAX_R_MM / u2mm if u2mm else _XSEC_DOT_MAX_R_MM
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
        cluster_gap = _XSEC_CLUSTER_GAP_MM / u2mm if u2mm else _XSEC_CLUSTER_GAP_MM
        cluster = _largest_cluster(nearby, max_gap=cluster_gap)
        if not cluster:
            continue

        # Detect bundle bars (closely-spaced dot pairs) and collapse each pair into one
        # unit — the schedule bundle factor counts pairs, so visual_count must be in
        # pair-units, matching what the vision path counts.
        is_bundle = _detect_bundles(cluster)
        units = _collapse_bundle_pairs(cluster) if is_bundle else cluster

        bar_count = len(units)
        # Pilecap bars are distributed across the section face in a grid, not arranged
        # around a perimeter ring — the ring-order chord algorithm produces false positives.
        # Spacing for pilecap is verified via the schedule c/c column instead.
        spacing_issues = (
            _compute_spacing_issues(units)
            if bar_count >= 3 and comp != 'pilecap'
            else []
        )

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


def _all_clusters(circles: list, max_gap: float) -> list:
    """Group circles into clusters where each member is within max_gap of another
    member of its own cluster (gap-based flood fill). Returns all clusters found,
    largest first."""
    if not circles:
        return []
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

    groups.sort(key=len, reverse=True)
    return groups


def _largest_cluster(circles: list, max_gap: float) -> list:
    """Return the largest group of circles where each is within max_gap of another."""
    clusters = _all_clusters(circles, max_gap)
    return clusters[0] if clusters else []


def _compute_spacing_issues(cluster: list) -> list:
    """
    Detect spacing irregularities for bars arranged in a ring, using ring-order
    chord (Euclidean) distance between consecutive bars rather than angular gap
    from the centroid. Angular gap is only uniform for a circular ring; on a
    rectangular pier cross-section the same correctly-spaced perimeter pitch
    subtends a larger angle near a corner than along a side (angle depends on
    distance from the centroid), and how much larger varies with the
    rectangle's aspect ratio — a fixed angular-gap multiplier that catches a
    corner defect on a near-square section can miss the identical defect on a
    more elongated one. Chord distance is shape-agnostic: bars are nominally
    spaced at a uniform pitch along the perimeter regardless of section shape,
    so comparing each gap to the ring's own median chord generalizes across
    circular and rectangular (or any convex) sections alike.
    """
    if len(cluster) < 4:
        return []

    cx = sum(c['x'] for c in cluster) / len(cluster)
    cy = sum(c['y'] for c in cluster) / len(cluster)

    ring = sorted(cluster, key=lambda c: math.atan2(c['y'] - cy, c['x'] - cx))
    n = len(ring)
    chords = [math.hypot(ring[(i + 1) % n]['x'] - ring[i]['x'],
                          ring[(i + 1) % n]['y'] - ring[i]['y']) for i in range(n)]
    median_chord = sorted(chords)[n // 2]
    if median_chord <= 0:
        return []

    issues = []
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        ratio = chords[i] / median_chord
        mid_angle = math.atan2((a['y'] + b['y']) / 2 - cy, (a['x'] + b['x']) / 2 - cx)
        clock_pos = _angle_to_clock(mid_angle)

        if ratio > _XSEC_GAP_RATIO:
            issues.append({
                'type': 'gap',
                'location': f'approx {clock_pos}',
                'description': f'Arc gap larger than expected (no bar between positions)',
                'angle_rad': mid_angle,
            })
        elif ratio < _XSEC_CLUSTER_RATIO:
            issues.append({
                'type': 'clustering',
                'location': f'approx {clock_pos}',
                'description': f'Bars unusually close together',
                'angle_rad': mid_angle,
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
    """
    Return True only when most dots form genuine touching pairs: each dot's
    nearest neighbour reciprocates (mutual 1-NN), and that pair distance is
    clearly smaller than each dot's *own* next-nearest spacing.

    The old version compared the single smallest pairwise distance in the
    whole cluster against the GLOBAL median pairwise distance. That breaks on
    a rectangular perimeter where different edges have different uniform bar
    pitches (e.g. 138.6mm c/c along the short edges vs. ~1580mm to dots on the
    far edge): the global median is dominated by long cross-cluster/diagonal
    distances, so the short edge's perfectly normal, non-bundled spacing looks
    "anomalously small" by comparison and the whole cluster gets misclassified
    as bundled. Confirmed on Section C-C/D-D (2026-06-22): 56 single dots at
    15+13 per face (matching the schedule's bar-count annotations and total
    count of 56) were wrongly halved to 32/31 "bundle pairs". The mutual-NN +
    per-point-ratio test only fires when dots are actually arranged as twins,
    regardless of how spacing varies between sides of a non-circular shape.
    """
    n = len(cluster)
    if n < 4:
        return False
    nn1_idx, nn1_d, nn2_d = [], [], []
    for i in range(n):
        ds = sorted(
            (math.hypot(cluster[i]['x'] - cluster[j]['x'], cluster[i]['y'] - cluster[j]['y']), j)
            for j in range(n) if j != i
        )
        nn1_d.append(ds[0][0])
        nn1_idx.append(ds[0][1])
        nn2_d.append(ds[1][0] if len(ds) > 1 else ds[0][0])
    # Mutual nearest-neighbour pairing: i's nearest is j AND j's nearest is i.
    mutual = sum(1 for i in range(n) if nn1_idx[nn1_idx[i]] == i)
    if mutual < n * 0.7:
        return False
    # For paired dots, the pair gap must be clearly smaller than that dot's
    # OWN spacing to the next-nearest (different) bar — not the cluster's
    # global stats, which vary by which edge/side a dot sits on.
    ratios = sorted(nn1_d[i] / nn2_d[i] for i in range(n) if nn2_d[i] > 0)
    if not ratios:
        return False
    median_ratio = ratios[len(ratios) // 2]
    return median_ratio < 0.6


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

# A ring within this gap (mm) of another ring belongs to the same physical view
# (e.g. several piles in one plan, or concentric ties in one section) — clustered
# before the label check runs so the check operates on the view's own footprint,
# not on each ring in isolation. Set above typical CASAD pile-to-pile spacing
# (3600mm confirmed on a real sheet) so a multi-pile plan clusters as one view
# instead of fragmenting into one cluster per pile — a fragment near the view's
# title would pass while a same-view fragment further from it would wrongly fail
# on its own, even though both belong to the one same (correctly or incorrectly)
# labeled view.
_VIEW_CLUSTER_GAP_MM = 4200.0

# A title counts as labelling a view only if it sits within this absolute distance
# of the view's own bounding box (expanded by this margin on every side) — not a
# fraction of the whole sheet. Calibrated against a real CASAD sheet with two
# visually-similar pile-plan views sharing one sheet, only one of which is actually
# captioned: the captioned one's nearest edge sits ~1300mm from its title, the
# uncaptioned one's nearest edge to that same (not-its-own) title is ~3160mm —
# confirmed by eye on the rendered drawing. Set between the two.
_VIEW_LABEL_MARGIN_MM = 2000.0

# Schedule "Shape of Bar" sketches for a circular bar mark (e.g. a confinement tie)
# are literal small circles sitting inside the schedule's own text cloud — excluded
# by bounding-box containment (schedule_bbox, from _extract_schedule) rather than by
# layer name, since CASAD doesn't consistently draw these on a REINF/SHAPE_BAR-named
# layer (confirmed on 02_DETAILS OF PILECAP-PIER_R1: identical shapes on plain
# 'S_MAIN'). Layer-name exclusion (_layer_excluded) stays as a first-line filter for
# the bent-bar (LWPOLYLINE) case it was built for; this is a second, independent net.
_SCHEDULE_EXCLUSION_MARGIN_MM = 500.0

# Title patterns strict enough to use as standalone labels (unlike a bare TRIGGER_WORDS
# substring match, which would flag 'LAP'/'NOTES' text sitting near a view as if it
# were that view's own title — the false positive the *_view_positions-based check
# used to guard against). Matched against each text item on its own, since CASAD
# titles are typically one atomic TEXT/MTEXT/attrib entity — no row-merging needed
# here, which sidesteps the row-merge bug where two titles sharing a y-band collapse
# into one anchor point and lose each other's true position.
_VIEW_TITLE_RE = re.compile(
    r'SECTION\s+[A-Z]\s*[\-\xad–]\s*[A-Z]|\bPLAN\s+OF\b|\bDETAIL[S]?\s+[A-Z]\b',
)


def _is_view_title_text(s: str) -> bool:
    return bool(_VIEW_TITLE_RE.search(s.upper().replace('\xad', '-').replace('–', '-')))


def _detect_unlabeled_section_circles(msp, all_text: list, extents: tuple,
                                       profile: DrawingTypeProfile = PPP_PROFILE,
                                       u2mm: float = 1.0,
                                       schedule_bbox: tuple | None = None) -> list:
    """
    Find large circular boundary shapes (pile / pilecap cross-section rings) in the views
    area that have no title label nearby, treating each physical view (a cluster of nearby
    rings, e.g. several piles in one plan) as one unit rather than judging each ring alone.
    Returns [{description, bbox}] for unlabeled_views.

    Looks for both CIRCLE primitives AND closed LWPOLYLINEs with roughly circular aspect
    ratio — CASAD engineers most often draw pile sections as closed LWPOLYLINEs, not CIRCLE.

    Candidates on REINF/SHAPE_BAR-style layers are excluded (`_layer_excluded`, shared with
    `_detect_component_regions`), and candidates falling inside the schedule's own text
    bounding box (`schedule_bbox`) are excluded independently of layer name — bent-bar and
    circular-tie shape sketches in the schedule's "Shape of Bar" column are closed shapes
    that can coincidentally pass the radius+aspect-ratio heuristic below.

    Labelling: rings are first clustered into views (`_VIEW_CLUSTER_GAP_MM`), then each
    view's own bounding box (not a single anchor point) is checked against nearby title
    text (`_is_view_title_text`) within `_VIEW_LABEL_MARGIN_MM` — an absolute distance from
    the view's real footprint, not a percentage of the whole sheet. This replaced an earlier
    single-point/fixed-sheet-fraction check that (a) relied on titles pre-merged into rows,
    which collapsed two separate titles sharing a y-band into one anchor and lost the other's
    true position, and (b) used one anchor point for a multi-ring view, so rings far from
    that one point could wrongly fail even when genuinely under the same title.
    """
    x_min, y_min, x_max, y_max = extents
    dw = x_max - x_min
    dh = y_max - y_min

    # Minimum radius for a "section boundary" shape.
    # 100mm absolute floor converted to drawing units via u2mm.
    # Rebar dots in CASAD DXFs are 5–20mm radius; pile sections are 300–600mm radius.
    min_sec_r = max(100.0 / u2mm, dh * 0.005)

    rings = []   # {x, y, r, kind}

    # Ring candidates are NOT gated by views_x_max_frac (removed 2026-06-24, same root
    # cause as the section-label/cut-letter gating bugs above): on ALL_SECTIONS-02.dxf
    # 9 of 11 real ring candidates sit past the 55% mark and were silently excluded
    # before ever reaching the "is this labeled?" check below — a position gate here
    # is actively dangerous for this function specifically, since its whole purpose is
    # catching genuinely-unlabeled rings; a candidate dropped before the label check
    # can never be flagged, a false negative in the system's core job. The downstream
    # label-proximity check (and radius/aspect filters above) already do the real
    # false-positive guarding, same as the other two fixes.
    try:
        for e in msp.query('CIRCLE'):
            if _layer_excluded(str(e.dxf.get('layer', ''))):
                continue
            cx, cy, cr = float(e.dxf.center.x), float(e.dxf.center.y), float(e.dxf.radius)
            if cr >= min_sec_r:
                rings.append({'x': cx, 'y': cy, 'r': cr, 'kind': 'circle'})
    except Exception:
        pass

    # --- Closed LWPOLYLINEs with roughly circular bounding box ---
    # Engineers draw pile cross-sections as closed polylines far more often than as CIRCLE.
    try:
        for e in msp.query('LWPOLYLINE'):
            if not e.is_closed:
                continue
            if _layer_excluded(str(e.dxf.get('layer', ''))):
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
            rings.append({'x': cx, 'y': cy, 'r': r_approx, 'kind': 'lwpoly'})
    except Exception:
        pass

    # Exclude candidates sitting inside the schedule's own text cloud (see
    # _SCHEDULE_EXCLUSION_MARGIN_MM docstring above).
    if schedule_bbox:
        sx0, sy0, sx1, sy1 = schedule_bbox
        sched_margin = _SCHEDULE_EXCLUSION_MARGIN_MM / u2mm
        before = len(rings)
        rings = [r for r in rings
                 if not (sx0 - sched_margin <= r['x'] <= sx1 + sched_margin
                         and sy0 - sched_margin <= r['y'] <= sy1 + sched_margin)]
        if len(rings) != before:
            log.info('Unlabeled-view scan: excluded %d ring candidate(s) inside the schedule '
                      'bounding box', before - len(rings))

    log.info('Unlabeled-view scan: %d candidate rings (CIRCLE+LWPOLY, r≥%.0f) in views area',
             len(rings), min_sec_r)
    if not rings:
        return []

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

    # Cluster rings into physical views before checking labels, so a multi-ring view
    # (several piles in one plan, or concentric ties in one section) is judged as a
    # whole rather than ring-by-ring against a single anchor point.
    cluster_gap = _VIEW_CLUSTER_GAP_MM / u2mm
    view_clusters = _all_clusters(unique_rings, cluster_gap)

    # Individual, unmerged title candidates — each kept at its own true (x, y) rather
    # than folded into a shared row, so two titles sharing a y-band (e.g. "SECTION X-X"
    # and "SECTION Y-Y" drawn side by side) don't erase each other's position.
    title_items = [t for t in all_text if _is_view_title_text(t['text'])]

    label_margin = _VIEW_LABEL_MARGIN_MM / u2mm

    def _nearest_title(bbox):
        bx0, by0, bx1, by1 = bbox
        best = None
        best_d = None
        for t in title_items:
            if not (bx0 - label_margin <= t['x'] <= bx1 + label_margin
                    and by0 - label_margin <= t['y'] <= by1 + label_margin):
                continue
            # distance from the title to the nearest edge of the view's own bbox
            dx = max(bx0 - t['x'], 0, t['x'] - bx1)
            dy = max(by0 - t['y'], 0, t['y'] - by1)
            d = math.hypot(dx, dy)
            if best_d is None or d < best_d:
                best_d, best = d, t
        return best

    unlabeled = []
    for cluster in view_clusters:
        bx0 = min(c['x'] - c['r'] for c in cluster)
        bx1 = max(c['x'] + c['r'] for c in cluster)
        by0 = min(c['y'] - c['r'] for c in cluster)
        by1 = max(c['y'] + c['r'] for c in cluster)
        cx = (bx0 + bx1) / 2
        cy = (by0 + by1) / 2
        x_pct = round((cx - x_min) / dw * 100, 1)
        y_pct = round((y_max - cy) / dh * 100, 1)

        title = _nearest_title((bx0, by0, bx1, by1))
        if title:
            log.info('View cluster at %.1f%%,%.1f%% (%d ring(s)) — confirmed label: %r',
                     x_pct, y_pct, len(cluster), title['text'][:60])
            continue

        log.info('Unlabeled view cluster at %.1f%%,%.1f%% (%d ring(s)) — no title within %.0f units',
                 x_pct, y_pct, len(cluster), label_margin)
        bbox = _to_bbox(bx0, by1, bx1, by0, extents)
        ring_note = '' if len(cluster) == 1 else f' ({len(cluster)} shapes in this view)'
        unlabeled.append({
            'description': (
                f'A structural cross-section ring is drawn at approximately '
                f'{x_pct}% across, {y_pct}% down the drawing but has no '
                f'"SECTION X-X" or similar title label nearby{ring_note}. '
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
    """Return presence status for each profile-required section view.

    Uses longest-match priority: a label L only satisfies required section S if
    no OTHER section's keyword (that is longer than S's keyword) also matches L.
    This prevents "REINFORCEMENT PLAN OF PILECAP" from satisfying the shorter
    "PLAN OF PILECAP" requirement via substring containment.
    """
    entries = list(profile.required_sections)
    result = []
    for name, keywords in entries:
        present = False
        bbox = None
        for label, pos in section_view_positions.items():
            lu = label.upper()
            matched_kw = next((kw for kw in keywords if kw.upper() in lu), None)
            if matched_kw is None:
                continue
            # Reject this label if another required section's keyword is longer and
            # also matches — that other section owns this label more specifically.
            longer_sibling = any(
                ok.upper() in lu and len(ok) > len(matched_kw)
                for oname, okws in entries if oname != name
                for ok in okws
            )
            if not longer_sibling:
                present = True
                bbox = pos
                break
        result.append({'name': name, 'present': present, 'bbox': bbox})
    return result


def _check_notes_completeness(all_text: list,
                              profile: DrawingTypeProfile = PPP_PROFILE) -> list:
    """Return presence status for each required note item via keyword scan of DXF text."""
    full_upper = ' '.join(t['text'] for t in all_text).upper()
    # Normalise non-ASCII hyphens (AutoCAD often writes soft hyphen / en-dash for "Fe-500")
    full_upper = full_upper.replace('\xad', '-').replace('–', '-').replace('—', '-')
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
