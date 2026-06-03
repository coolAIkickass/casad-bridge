"""
Bridge Checker — quality-check and auto-correct bridge inspection reports
before delivery via WhatsApp.

Public API (called by server.py):
  check_report(out_path, report_json, fmt, raw_bridge_details=None)
      -> CheckResult
  correct_report(out_path, report_json, fmt, check_result)
      -> corrected out_path (str)
  log_issues(check_result, phone, bridge_name, fmt)
      -> None

TODO: implement the checks below. The stubs raise NotImplementedError so
      server.py's try/except silently skips them until they are built.

──────────────────────────────────────────────────────────────────────────
BELOW IS THE ORIGINAL ED CHECKER CODE that previously lived in checker/.
Kept here for reference while the bridge checker is built.
──────────────────────────────────────────────────────────────────────────

# import os
# import logging
# from .excel_parser import parse_e2e_excel
# from .pdf_extractor import extract_from_drawing
# from .comparator import compare
#
# log = logging.getLogger(__name__)
#
# ACCEPTED_EXCEL = ('.xlsx', '.xls')
# ACCEPTED_IMAGE = ('.jpg', '.jpeg', '.png')
# ACCEPTED_PDF   = ('.pdf',)
#
#
# def parse_design_inputs(design_files: list) -> tuple:
#     design_data = {}
#     parse_errors = []
#     for fname, fbytes in (design_files or []):
#         ext = _ext(fname)
#         if ext in ACCEPTED_EXCEL:
#             try:
#                 parsed = parse_e2e_excel(fbytes)
#                 for k, v in parsed.items():
#                     if v:
#                         design_data[k] = v
#             except Exception as e:
#                 parse_errors.append(f'{fname}: {e}')
#                 log.warning('Design input parse error — %s: %s', fname, e)
#     return design_data, parse_errors
#
#
# def detect_drawing_type(drawing_data: dict) -> str:
#     schedule   = drawing_data.get('schedule', {})
#     sections   = drawing_data.get('sections', []) or []
#     section_names = ' '.join((s.get('name') or '') for s in sections).upper()
#     title      = (drawing_data.get('title_block', {}).get('title') or '').upper()
#     all_text = section_names + ' ' + title
#     if 'pile' in schedule or 'pilecap' in schedule or 'PILE' in all_text:
#         return 'Pile Pilecap Pier'
#     if 'ABUTMENT' in all_text:
#         return 'Abutment'
#     if 'SUPERSTRUCTURE' in all_text or 'GIRDER' in all_text or 'DECK' in all_text:
#         return 'Superstructure'
#     if 'BEARING' in all_text:
#         return 'Bearing'
#     return 'General'
#
#
# def run_check(drawing_pdf_bytes: bytes, design_data: dict) -> tuple:
#     drawing_data = extract_from_drawing(drawing_pdf_bytes)
#     api_key_missing = not os.environ.get('ANTHROPIC_API_KEY', '').strip()
#     vision_ran = bool(drawing_data.get('schedule'))
#     if api_key_missing and not vision_ran:
#         issues = [{ 'category': 'Configuration', ... }]
#         text_only = compare(design_data or None, drawing_data)
#         issues += [i for i in text_only if i.get('category') not in ('Reinforcement', 'Levels (TABLE-1)')]
#     elif not vision_ran:
#         issues = [{ 'category': 'Configuration', ... }]
#         text_only = compare(design_data or None, drawing_data)
#         issues += [i for i in text_only if i.get('category') not in ('Reinforcement', 'Levels (TABLE-1)')]
#     else:
#         issues = compare(design_data or None, drawing_data)
#     detected_type = detect_drawing_type(drawing_data)
#     return issues, detected_type
#
#
# def _ext(filename: str) -> str:
#     return os.path.splitext(filename.lower())[1]
"""


# ── Data classes ──────────────────────────────────────────────────────────────

class Issue:
    """A single quality issue found in a bridge inspection report."""
    def __init__(self, rule, severity, cell_or_field, description='', was_corrected=False):
        self.rule          = rule
        self.severity      = severity        # 'error' | 'warning'
        self.cell_or_field = cell_or_field
        self.description   = description
        self.was_corrected = was_corrected


class CheckResult:
    """Result returned by check_report()."""
    def __init__(self, issues=None):
        self.issues = issues or []

    @property
    def has_issues(self):
        return len(self.issues) > 0


# ── Public API stubs ──────────────────────────────────────────────────────────

def check_report(out_path, report_json, fmt, raw_bridge_details=None):
    """
    Check a generated bridge inspection report for quality issues.
    TODO: implement checks (missing fields, inconsistent values, formatting errors).
    """
    raise NotImplementedError('bridge_checker.check_report not yet implemented')


def correct_report(out_path, report_json, fmt, check_result):
    """
    Auto-correct fixable issues in the report and return the new file path.
    TODO: implement corrections.
    """
    raise NotImplementedError('bridge_checker.correct_report not yet implemented')


def log_issues(check_result, phone, bridge_name, fmt):
    """
    Log check results for auditing.
    TODO: implement logging to DB or file.
    """
    raise NotImplementedError('bridge_checker.log_issues not yet implemented')
