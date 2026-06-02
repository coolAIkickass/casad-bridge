"""
ED Checker — main entry point.
run_check(drawing_pdf_bytes, design_files) -> list of issue dicts
"""
import os
from .excel_parser import parse_e2e_excel
from .pdf_extractor import extract_from_drawing
from .comparator import compare


ACCEPTED_EXCEL = ('.xlsx', '.xls')
ACCEPTED_IMAGE = ('.jpg', '.jpeg', '.png')
ACCEPTED_PDF   = ('.pdf',)


def run_check(drawing_pdf_bytes: bytes, design_files: list) -> list:
    """
    drawing_pdf_bytes : raw bytes of the drawing PDF
    design_files      : list of (filename: str, file_bytes: bytes)

    Returns list of issue dicts compatible with the DB issues table.
    """
    design_data = {}

    for fname, fbytes in (design_files or []):
        ext = _ext(fname)
        if ext in ACCEPTED_EXCEL:
            try:
                parsed = parse_e2e_excel(fbytes)
                # Merge — later files overwrite earlier for same keys
                for k, v in parsed.items():
                    if v:
                        design_data[k] = v
            except Exception as e:
                design_data.setdefault('_parse_errors', []).append(f'{fname}: {e}')
        # Reference PDFs / JPEGs: stored for future use; not parsed in this version

    drawing_data = extract_from_drawing(drawing_pdf_bytes)

    # If vision extraction didn't run (no API key), warn once clearly instead of
    # generating confusing "schedule not found" warnings per component
    api_key_missing = not os.environ.get('ANTHROPIC_API_KEY', '').strip()
    vision_ran = bool(drawing_data.get('schedule'))

    if api_key_missing and not vision_ran:
        issues = [{
            'category': 'Configuration',
            'title': 'AI vision check not available — ANTHROPIC_API_KEY not configured',
            'description': (
                'The schedule tables, TABLE-1 levels, and notes in the drawing could not be '
                'checked because the AI vision API key is not set on this server. '
                'Title block format checks ran successfully from text extraction.'
            ),
            'suggestion': 'Set the ANTHROPIC_API_KEY environment variable on the Render service to enable full AI-powered checks.',
            'severity': 'warning', 'page_num': 1,
            'x': 5, 'y': 5, 'width': 90, 'height': 10,
        }]
        # Still run title block and notes checks (text-only)
        text_only_issues = compare(design_data if design_data else None, drawing_data)
        # Keep only title block / notes issues (those don't need vision)
        issues += [i for i in text_only_issues if i.get('category') not in ('Reinforcement', 'Levels (TABLE-1)')]
    else:
        issues = compare(design_data if design_data else None, drawing_data)

    # Surface any parse errors as warnings
    for err in design_data.get('_parse_errors', []):
        issues.append({
            'category': 'Input', 'title': 'Design input parse error',
            'description': f'Could not parse design input: {err}',
            'suggestion': 'Check that the Excel file matches the CASAD E2E BBS format.',
            'severity': 'warning', 'page_num': 1,
            'x': 5, 'y': 5, 'width': 30, 'height': 8,
        })

    return issues


def _ext(filename: str) -> str:
    import os
    return os.path.splitext(filename.lower())[1]
