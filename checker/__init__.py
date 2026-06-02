"""
ED Checker — main entry point.
run_check(drawing_pdf_bytes, design_files) -> list of issue dicts
"""
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
