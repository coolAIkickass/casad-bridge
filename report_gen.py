# report_gen.py — python-docx: JSON → filled .docx report
import os
from copy import deepcopy
from docx import Document
from docx.shared import Inches

TEMPLATE_PATH = os.getenv('TEMPLATE_PATH', 'casad_template.docx')
OUTPUT_DIR    = os.getenv('OUTPUT_DIR', 'media')


def _fill_placeholders(doc: Document, data: dict, prefix: str = '') -> None:
    """Recursively replace {{key}} placeholders in all paragraphs and table cells."""
    flat = _flatten(data, prefix)
    for para in doc.paragraphs:
        for key, val in flat.items():
            if f'{{{{{key}}}}}' in para.text:
                for run in para.runs:
                    run.text = run.text.replace(f'{{{{{key}}}}}', str(val))
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    for key, val in flat.items():
                        if f'{{{{{key}}}}}' in para.text:
                            for run in para.runs:
                                run.text = run.text.replace(f'{{{{{key}}}}}', str(val))


def _flatten(d: dict, parent: str = '') -> dict:
    """Flatten nested dict to dot-notation keys."""
    items = {}
    for k, v in d.items():
        full_key = f'{parent}.{k}' if parent else k
        if isinstance(v, dict):
            items.update(_flatten(v, full_key))
        elif isinstance(v, list):
            items[full_key] = ', '.join(str(i) for i in v)
        else:
            items[full_key] = v or ''
    return items


def build_docx(report_json: dict) -> str:
    """Fill CASAD template with report_json and return saved file path."""
    doc = Document(TEMPLATE_PATH)
    _fill_placeholders(doc, report_json)

    # Append photo appendix
    photos = report_json.get('photos', [])
    if photos:
        doc.add_page_break()
        doc.add_heading('Appendix — Site Photographs', level=1)
        for i, photo_path in enumerate(photos, 1):
            if os.path.exists(photo_path):
                doc.add_paragraph(f'Photo {i}')
                doc.add_picture(photo_path, width=Inches(5))

    bridge = report_json.get('bridge_name', 'bridge').replace(' ', '_')
    date   = report_json.get('date_of_survey', 'report').replace('/', '-')
    out_path = os.path.join(OUTPUT_DIR, f'CASAD_{bridge}_{date}.docx')
    doc.save(out_path)
    return out_path
