# report_gen.py — python-docx: JSON → filled .docx report
import os, re
from docx import Document
from docx.shared import Inches, Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

TEMPLATE_PATH = os.getenv('TEMPLATE_PATH', 'casad_template.docx')
OUTPUT_DIR    = os.getenv('OUTPUT_DIR', 'media')


def _fill_placeholders(doc: Document, data: dict) -> None:
    """Replace {{key}} placeholders in all paragraphs and table cells."""
    flat = {}
    for k, v in data.items():
        if isinstance(v, list):
            flat[k] = '\n'.join(str(i) for i in v)
        elif isinstance(v, dict):
            for sk, sv in v.items():
                flat[f'{k}.{sk}'] = str(sv) if sv else ''
        else:
            flat[k] = str(v) if v else ''

    def replace_in_para(para):
        full_text = ''.join(r.text for r in para.runs)
        if '{{' not in full_text:
            return
        new_text = full_text
        for key, val in flat.items():
            new_text = new_text.replace(f'{{{{{key}}}}}', val)
        if new_text != full_text and para.runs:
            para.runs[0].text = new_text
            for run in para.runs[1:]:
                run.text = ''

    for para in doc.paragraphs:
        replace_in_para(para)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    replace_in_para(para)


def _insert_photos_at_marker(doc: Document, marker: str, photos: list) -> None:
    """Find the marker paragraph and replace it with photo images + captions."""
    target = None
    for para in doc.paragraphs:
        if para.text.strip() == marker:
            target = para
            break
    if target is None:
        return

    parent = target._element.getparent()
    insert_pos = list(parent).index(target._element)

    # Remove the marker paragraph
    parent.remove(target._element)

    for i, photo_path in enumerate(photos, 1):
        if not photo_path or not os.path.exists(photo_path):
            continue

        # Caption paragraph
        cap_p = OxmlElement('w:p')
        cap_pPr = OxmlElement('w:pPr')
        cap_jc = OxmlElement('w:jc')
        cap_jc.set(qn('w:val'), 'center')
        cap_pPr.append(cap_jc)
        cap_p.append(cap_pPr)
        cap_r = OxmlElement('w:r')
        cap_rPr = OxmlElement('w:rPr')
        cap_b = OxmlElement('w:b')
        cap_rPr.append(cap_b)
        cap_r.append(cap_rPr)
        cap_t = OxmlElement('w:t')
        cap_t.text = f'Figure {i}'
        cap_r.append(cap_t)
        cap_p.append(cap_r)
        parent.insert(insert_pos, cap_p)
        insert_pos += 1

        # Image paragraph — build via a temporary in-memory Document
        tmp = Document()
        pic_para = tmp.add_paragraph()
        pic_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        pic_para.add_run().add_picture(photo_path, width=Inches(5.5))
        pic_elem = pic_para._element
        parent.insert(insert_pos, pic_elem)
        insert_pos += 1

        # Page break after every 2 photos (except the last)
        if i % 2 == 0 and i < len(photos):
            pb_p = OxmlElement('w:p')
            pb_r = OxmlElement('w:r')
            pb_br = OxmlElement('w:br')
            pb_br.set(qn('w:type'), 'page')
            pb_r.append(pb_br)
            pb_p.append(pb_r)
            parent.insert(insert_pos, pb_p)
            insert_pos += 1


def build_docx(report_json: dict) -> str:
    """Fill CASAD template with report_json and return saved file path."""
    doc = Document(TEMPLATE_PATH)
    _fill_placeholders(doc, report_json)

    photos = [p for p in report_json.get('photos', []) if p and os.path.exists(p)]

    if photos:
        # Split photos: first half → Appendix A (general), rest → Appendix B (damage)
        mid = len(photos) // 2 or len(photos)
        _insert_photos_at_marker(doc, '[[PHOTO_APPENDIX_A]]', photos[:mid])
        _insert_photos_at_marker(doc, '[[PHOTO_APPENDIX_B]]', photos[mid:] or photos)
    else:
        # No photos submitted — replace markers with a note
        for marker in ('[[PHOTO_APPENDIX_A]]', '[[PHOTO_APPENDIX_B]]'):
            for para in doc.paragraphs:
                if para.text.strip() == marker:
                    para.runs[0].text = 'No photographs submitted.'
                    break

    river    = re.sub(r'[^\w\-]', '_', report_json.get('river_name', 'bridge'))
    road     = re.sub(r'[^\w\-]', '_', report_json.get('road_name',  'road'))
    date     = report_json.get('date_of_survey', 'report').replace('/', '-')
    out_path = os.path.join(OUTPUT_DIR, f'CASAD_{river}_{road}_{date}.docx')
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    doc.save(out_path)
    return out_path
