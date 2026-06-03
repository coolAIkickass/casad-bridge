"""
ED Checker — main entry point.

Public API:
  parse_design_inputs(design_files)          -> (design_data dict, parse_errors list)
  run_check(drawing_pdf_bytes, design_data)  -> list of issue dicts
"""
import os
import logging
from .excel_parser import parse_e2e_excel
from .pdf_extractor import extract_from_drawing
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
    sections   = drawing_data.get('sections', []) or []
    section_names = ' '.join((s.get('name') or '') for s in sections).upper()
    title      = (drawing_data.get('title_block', {}).get('title') or '').upper()

    all_text = section_names + ' ' + title

    if 'pile' in schedule or 'pilecap' in schedule or 'PILE' in all_text:
        return 'Pile Pilecap Pier'
    if 'ABUTMENT' in all_text:
        return 'Abutment'
    if 'SUPERSTRUCTURE' in all_text or 'GIRDER' in all_text or 'DECK' in all_text:
        return 'Superstructure'
    if 'BEARING' in all_text:
        return 'Bearing'
    return 'General'


def run_check(drawing_pdf_bytes: bytes, design_data: dict) -> tuple:
    """
    drawing_pdf_bytes : raw bytes of the drawing PDF
    design_data       : already-parsed design data dict (from parse_design_inputs)
                        pass {} or None if no design input was provided

    Returns list of issue dicts compatible with the DB issues table.
    """
    drawing_data = extract_from_drawing(drawing_pdf_bytes)

    # Detect if vision ran or was skipped
    api_key_missing = not os.environ.get('ANTHROPIC_API_KEY', '').strip()
    vision_ran = bool(drawing_data.get('schedule'))

    if not vision_ran:
        log.warning(
            'Vision extraction returned no schedule data. '
            'api_key_missing=%s raw_text_lines=%d',
            api_key_missing,
            len(drawing_data.get('raw_text', []))
        )

    if api_key_missing and not vision_ran:
        # One clear message instead of per-component "not found" warnings
        issues = [{
            'category': 'Configuration',
            'title': 'AI vision check not available — ANTHROPIC_API_KEY not set',
            'description': (
                'Schedule tables, TABLE-1 levels, and notes could not be checked because '
                'the AI vision API key is not configured on this server. '
                'Title block format checks ran from text extraction.'
            ),
            'suggestion': 'Set ANTHROPIC_API_KEY in the Render service environment variables.',
            'severity': 'warning', 'page_num': 1,
            'x': 5, 'y': 5, 'width': 90, 'height': 10,
        }]
        text_only = compare(design_data or None, drawing_data)
        issues += [i for i in text_only if i.get('category') not in ('Reinforcement', 'Levels (TABLE-1)')]

    elif not vision_ran:
        # API key is set but vision still failed — surface a diagnostic warning
        issues = [{
            'category': 'Configuration',
            'title': 'AI vision extraction failed',
            'description': (
                'The ANTHROPIC_API_KEY is configured but the AI vision check returned no data. '
                'This may be caused by a PDF rendering error (PyMuPDF) or an API timeout. '
                'Check server logs for details.'
            ),
            'suggestion': 'Check Render logs for errors from checker/pdf_extractor.py.',
            'severity': 'warning', 'page_num': 1,
            'x': 5, 'y': 5, 'width': 90, 'height': 10,
        }]
        text_only = compare(design_data or None, drawing_data)
        issues += [i for i in text_only if i.get('category') not in ('Reinforcement', 'Levels (TABLE-1)')]

    else:
        issues = compare(design_data or None, drawing_data)

    detected_type = detect_drawing_type(drawing_data)
    return issues, detected_type


def _ext(filename: str) -> str:
    return os.path.splitext(filename.lower())[1]
