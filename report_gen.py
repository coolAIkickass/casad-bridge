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


def _insert_photos_at_marker(doc: Document, marker: str, photos: list, captions: list = None) -> None:
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
    parent.remove(target._element)

    for i, photo_path in enumerate(photos, 1):
        if not photo_path or not os.path.exists(photo_path):
            continue

        # Image — add to doc first (registers relationship), then move
        pic_para = doc.add_paragraph()
        pic_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        pic_para.add_run().add_picture(photo_path, width=Inches(5.5))
        pic_elem = pic_para._element
        parent.remove(pic_elem)
        parent.insert(insert_pos, pic_elem)
        insert_pos += 1

        # Caption: "Figure 1: user comment" below the photo
        user_caption = (captions[i - 1] if captions and i - 1 < len(captions) else '') or ''
        cap_text = f'Figure {i}'
        if user_caption:
            cap_text += f': {user_caption}'
        cap_para = doc.add_paragraph()
        cap_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap_run = cap_para.add_run(cap_text)
        cap_run.italic = True
        cap_run.font.size = Pt(10)
        cap_elem = cap_para._element
        parent.remove(cap_elem)
        parent.insert(insert_pos, cap_elem)
        insert_pos += 1

        # Page break after every 2 photos (except the last)
        if i % 2 == 0 and i < len(photos):
            pb_para = doc.add_paragraph()
            pb_para.add_run().add_break()
            pb_elem = pb_para._element
            parent.remove(pb_elem)
            parent.insert(insert_pos, pb_elem)
            insert_pos += 1


def build_docx(report_json: dict) -> str:
    """Fill CASAD template with report_json and return saved file path."""
    doc = Document(TEMPLATE_PATH)
    _fill_placeholders(doc, report_json)

    raw_photos   = report_json.get('photos', [])
    raw_captions = report_json.get('photo_captions', [])

    # Keep only photos whose files exist, carrying captions along
    pairs = [(p, c) for p, c in zip(raw_photos, raw_captions + [''] * len(raw_photos))
             if p and os.path.exists(p)]
    photos   = [p for p, _ in pairs]
    captions = [c for _, c in pairs]
    print(f"BUILD_DOCX usable photos: {photos}")

    if photos:
        _insert_photos_at_marker(doc, '[[PHOTO_APPENDIX]]', photos, captions)
    else:
        for para in doc.paragraphs:
            if para.text.strip() == '[[PHOTO_APPENDIX]]':
                para.runs[0].text = 'No photographs submitted.'
                break

    river    = re.sub(r'[^\w\-]', '_', report_json.get('river_name', 'bridge'))
    road     = re.sub(r'[^\w\-]', '_', report_json.get('road_name',  'road'))
    date     = report_json.get('date_of_survey', 'report').replace('/', '-')
    out_path = os.path.join(OUTPUT_DIR, f'CASAD_{river}_{road}_{date}.docx')
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    doc.save(out_path)
    return out_path
