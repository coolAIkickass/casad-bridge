"""
ED Checker — main entry point.

Public API:
  parse_design_inputs(design_files)                        -> (design_data dict, parse_errors list)
  run_check(drawing_pdf_bytes, design_data, dxf_bytes)     -> (issues list, detected_type str)
"""
import gc
import os
import re
import logging
from .excel_parser import parse_e2e_excel
from .pdf_extractor import extract_from_drawing, _extract_text as _pdf_extract_text
from .pdf_extractor import _text_missing_sections, run_review_vision
from .comparator import compare

log = logging.getLogger(__name__)

ACCEPTED_EXCEL = ('.xlsx', '.xls')
ACCEPTED_IMAGE = ('.jpg', '.jpeg', '.png')
ACCEPTED_PDF   = ('.pdf',)


def parse_design_inputs(design_files: list) -> tuple:
    """
    Parse a list of (filename, bytes) design input files.
    Returns (design_data dict, parse_errors list).
    design_data is JSON-serialisable and can be stored in the DB for reuse on re-uploads.
    """
    design_data = {}
    parse_errors = []

    for fname, fbytes in (design_files or []):
        ext = _ext(fname)
        if ext in ACCEPTED_EXCEL:
            try:
                parsed = parse_e2e_excel(fbytes)
                for k, v in parsed.items():
                    if v:
                        design_data[k] = v
            except Exception as e:
                parse_errors.append(f'{fname}: {e}')
                log.warning('Design input parse error — %s: %s', fname, e)
        # Reference PDFs / JPEGs: not parsed in this version

    return design_data, parse_errors


def detect_drawing_type(drawing_data: dict) -> str:
    """Infer drawing type from extracted drawing content."""
    schedule   = drawing_data.get('schedule', {})
    sections   = drawing_data.get('sections_from_text', []) or []
    section_names = ' '.join((s.get('name') or '') for s in sections if s.get('present')).upper()
    view_labels   = ' '.join(drawing_data.get('section_view_positions', {}) or {}).upper()
    title      = (drawing_data.get('title_block', {}).get('title') or '').upper()

    all_text = section_names + ' ' + view_labels + ' ' + title

    if 'pile' in schedule or 'pilecap' in schedule or 'PILE' in all_text:
        return 'Pile Pilecap Pier'
    if 'ABUTMENT' in all_text:
        return 'Abutment'
    if 'SUPERSTRUCTURE' in all_text or 'GIRDER' in all_text or 'DECK' in all_text:
        return 'Superstructure'
    if 'BEARING' in all_text:
        return 'Bearing'
    return 'General'


def run_check(drawing_pdf_bytes: bytes, design_data: dict,
              dxf_bytes: bytes = None) -> tuple:
    """
    drawing_pdf_bytes : raw bytes of the drawing PDF (required — used for display)
    design_data       : already-parsed design data dict (from parse_design_inputs)
                        pass {} or None if no design input was provided
    dxf_bytes         : optional AutoCAD DXF bytes; when provided, exact DXF extraction
                        replaces Claude vision for schedule, title block, notes, TABLE-1,
                        and cross-section bar counting

    Returns (issues list, detected_type str).
    """
    if dxf_bytes:
        drawing_data, vision_ran = _run_dxf_extraction(drawing_pdf_bytes, dxf_bytes)
    else:
        drawing_data = extract_from_drawing(drawing_pdf_bytes)
        api_key_missing = not os.environ.get('ANTHROPIC_API_KEY', '').strip()
        vision_ran = bool(drawing_data.get('schedule'))

        if not vision_ran:
            log.warning(
                'Vision extraction returned no schedule data. '
                'api_key_missing=%s raw_text_lines=%d',
                api_key_missing,
                len(drawing_data.get('raw_text', []))
            )

    if dxf_bytes:
        # DXF path: schedule extraction is exact — no vision fallback needed
        if not vision_ran:
            issues = [{
                'category': 'Configuration',
                'title': 'DXF extraction returned no schedule data',
                'description': (
                    'The uploaded DXF file was parsed but no reinforcement schedule was found. '
                    'Ensure the DXF contains the schedule in the right portion of the drawing '
                    'and that text entities are present (not just lines).'
                ),
                'suggestion': 'Check that the DXF was exported with text (not as outlines). '
                              'Try File > Save As > AutoCAD DXF in AutoCAD.',
                'severity': 'error', 'page_num': 1,
                'x': 5, 'y': 5, 'width': 90, 'height': 10,
            }]
            issues += compare(design_data or None, drawing_data)
        else:
            issues = compare(design_data or None, drawing_data)

    elif not os.environ.get('ANTHROPIC_API_KEY', '').strip() and not vision_ran:
        issues = [{
            'category': 'Configuration',
            'title': 'AI vision check not available — ANTHROPIC_API_KEY not set',
            'description': (
                'Schedule tables, TABLE-1 levels, and notes could not be checked because '
                'the AI vision API key is not configured on this server. '
                'Title block format checks ran from text extraction. '
                'Alternatively, upload an AutoCAD DXF file for exact schedule extraction '
                'without needing the vision API.'
            ),
            'suggestion': 'Set ANTHROPIC_API_KEY in the Render service environment variables, '
                          'or upload a DXF file alongside the PDF.',
            'severity': 'warning', 'page_num': 1,
            'x': 5, 'y': 5, 'width': 90, 'height': 10,
        }]
        text_only = compare(design_data or None, drawing_data)
        issues += [i for i in text_only if i.get('category') not in ('Reinforcement', 'Levels (TABLE-1)')]

    elif not vision_ran:
        issues = [{
            'category': 'Configuration',
            'title': 'AI vision extraction failed',
            'description': (
                'The ANTHROPIC_API_KEY is configured but the AI vision check returned no data. '
                'This may be caused by a PDF rendering error (PyMuPDF) or an API timeout. '
                'Alternatively, upload an AutoCAD DXF file for exact schedule extraction.'
            ),
            'suggestion': 'Check Render logs for errors from ed_checker/pdf_extractor.py, '
                          'or upload a DXF file alongside the PDF.',
            'severity': 'warning', 'page_num': 1,
            'x': 5, 'y': 5, 'width': 90, 'height': 10,
        }]
        text_only = compare(design_data or None, drawing_data)
        issues += [i for i in text_only if i.get('category') not in ('Reinforcement', 'Levels (TABLE-1)')]

    else:
        issues = compare(design_data or None, drawing_data)

    detected_type = detect_drawing_type(drawing_data)
    return issues, detected_type


def _run_dxf_extraction(pdf_bytes: bytes, dxf_bytes: bytes) -> tuple:
    """
    Run DXF extraction and merge pdfplumber position data.
    Returns (drawing_data dict, vision_ran bool).
    vision_ran is True when the DXF schedule was non-empty.
    """
    from .dxf_extractor import extract_from_dxf

    drawing_data = extract_from_dxf(dxf_bytes)

    # Always run pdfplumber on the PDF — it gives accurate PDF-coordinate positions
    # for marker placement on the review UI, and supplements completeness checks.
    try:
        text_data = _pdf_extract_text(pdf_bytes)

        # Override schedule_section_positions — these must be PDF coordinates
        drawing_data['schedule_section_positions'] = text_data.get('schedule_section_positions', {})

        # Merge section_view_positions: start with DXF labels, overlay pdfplumber labels
        # (pdfplumber uses PDF coordinates which are correct for marker placement).
        dxf_sv = drawing_data.get('section_view_positions', {})
        pdf_sv = text_data.get('section_view_positions', {})
        merged_sv = {**dxf_sv, **pdf_sv}   # pdf takes precedence for shared keys
        drawing_data['section_view_positions'] = merged_sv

        # Prefer pdfplumber sections_from_text but patch any present=False entries
        # using the combined section_view_positions — pdfplumber can miss sections
        # that are text-underlined (%%U codes) or use non-standard PDF encoding.
        if text_data.get('sections_from_text'):
            sft = text_data['sections_from_text']
            sv_upper = {k.upper() for k in merged_sv}
            for entry in sft:
                if not entry.get('present'):
                    name_u = entry['name'].upper()
                    # Check if any keyword from this section appears in merged sv keys
                    if any(name_u in sv_key or sv_key in name_u for sv_key in sv_upper):
                        entry['present'] = True
                        log.info('sections_from_text: patched %r to present=True via DXF', entry['name'])
            drawing_data['sections_from_text'] = sft
        if text_data.get('notes_completeness_from_text'):
            drawing_data['notes_completeness_from_text'] = text_data['notes_completeness_from_text']

        # Supplement title block and notes gaps from pdfplumber (belt-and-suspenders)
        for key, val in text_data.get('title_block', {}).items():
            if not drawing_data['title_block'].get(key):
                drawing_data['title_block'][key] = val
        for key, val in text_data.get('notes', {}).items():
            if not drawing_data['notes'].get(key):
                drawing_data['notes'][key] = val

        # DXF path: trust DXF cut_letters exclusively when non-empty.
        # pdfplumber is unreliable for AutoCAD PDFs — AutoCAD sometimes stores text
        # with per-character positioning, causing pdfplumber to return individual
        # characters (e.g. 'C' from 'PILECAP', 'D' from 'CHECKED') as separate
        # "words", each counted as a single-letter occurrence. This inflates cut_letter
        # counts, producing false-positive SECTION C-C / SECTION D-D missing-view errors.
        # DXF TEXT entities are whole strings, so DXF cut_letters are reliable.
        # Only fall back to pdfplumber when DXF found zero cut letters.
        if not drawing_data.get('cut_letters') and text_data.get('cut_letters'):
            drawing_data['cut_letters'] = text_data['cut_letters']
        # Do NOT union: pdfplumber false letters must not pollute DXF-detected cut_letters.

    except Exception as e:
        log.warning('pdfplumber merge failed: %s', e)
        drawing_data.setdefault('extraction_diagnostics', []).append({
            'code': 'pdfplumber_merge_failed',
            'message': (
                f'PDF text positions could not be merged ({e}). Issue markers may be '
                f'misplaced on the review viewer and some completeness checks may report '
                f'false missing-view errors.'
            ),
            'severity': 'error',
        })

    # Compute cut-mark cross-reference using merged cut_letters + section_view_positions
    cut_letters      = drawing_data.get('cut_letters', set())
    sv_pos           = drawing_data.get('section_view_positions', {})
    drawing_data['missing_referenced_sections'] = _text_missing_sections(cut_letters, sv_pos)

    # Free ezdxf objects before rendering PDF — ezdxf has cyclic refs that refcounting
    # won't reclaim, so the parsed DXF (~100-200 MB) stays alive until gc.collect().
    # Running gc here prevents OOM when the 2.0× PyMuPDF render follows immediately.
    gc.collect()

    # Run the visual review pass (CHECK 3–6) even in DXF path.
    # DXF text extraction cannot detect unlabeled views, stray boxes,
    # missing dimensions, or label/annotation quality issues.
    try:
        _section_labels = sorted(drawing_data.get('section_view_positions', {}).keys())
        _missing_secs   = drawing_data.get('missing_referenced_sections', [])
        review_data = run_review_vision(pdf_bytes, _section_labels, _missing_secs)
        if review_data:
            drawing_data['label_issues']         = review_data.get('label_issues')         or []
            drawing_data['dimension_issues']     = review_data.get('dimension_issues')     or []
            drawing_data['cross_section_checks'] = (
                drawing_data.get('cross_section_checks') or
                review_data.get('cross_section_checks') or []
            )
            drawing_data['erroneous_boxes']      = review_data.get('erroneous_boxes')      or []
            drawing_data['unlabeled_views']      = review_data.get('unlabeled_views')      or []
    except Exception as e:
        log.warning('DXF path: review vision call failed: %s', e)

    vision_ran = bool(drawing_data.get('schedule'))
    return drawing_data, vision_ran


def _ext(filename: str) -> str:
    return os.path.splitext(filename.lower())[1]
