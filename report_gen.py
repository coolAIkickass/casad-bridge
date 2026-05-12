# report_gen.py — python-docx: JSON → filled .docx report
import os, re, copy
from docx import Document
from docx.shared import Inches, Pt, RGBColor, Cm
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


def _add_bookmark(para, bookmark_id: int, name: str) -> None:
    """Wrap paragraph content in a named bookmark."""
    p = para._p
    bm_start = OxmlElement('w:bookmarkStart')
    bm_start.set(qn('w:id'), str(bookmark_id))
    bm_start.set(qn('w:name'), name)
    bm_end = OxmlElement('w:bookmarkEnd')
    bm_end.set(qn('w:id'), str(bookmark_id))
    p.insert(0, bm_start)
    p.append(bm_end)


def _replace_photo_refs_with_hyperlinks(doc: Document) -> None:
    """Replace '(Photo No.-X)' text in table cells with internal bookmark hyperlinks."""
    import re
    pattern = re.compile(r'(\(Photo No\.-\d+\))')

    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    full_text = ''.join(r.text for r in para.runs)
                    if '(Photo No.-' not in full_text:
                        continue

                    # Capture run formatting from first run
                    p = para._p
                    first_rpr = None
                    first_r = p.find(qn('w:r'))
                    if first_r is not None:
                        first_rpr = first_r.find(qn('w:rPr'))

                    # Remove all existing runs
                    for r_el in p.findall(qn('w:r')):
                        p.remove(r_el)

                    # Rebuild paragraph — plain text + hyperlink elements
                    for part in pattern.split(full_text):
                        if not part:
                            continue
                        m = re.match(r'\(Photo No\.-(\d+)\)', part)
                        if m:
                            fig_num  = m.group(1)
                            anchor   = f'figure_{fig_num}'
                            hl = OxmlElement('w:hyperlink')
                            hl.set(qn('w:anchor'), anchor)
                            r_el = OxmlElement('w:r')
                            rPr  = OxmlElement('w:rPr')
                            rs   = OxmlElement('w:rStyle')
                            rs.set(qn('w:val'), 'Hyperlink')
                            rPr.append(rs)
                            r_el.append(rPr)
                            t_el = OxmlElement('w:t')
                            t_el.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                            t_el.text = part
                            r_el.append(t_el)
                            hl.append(r_el)
                            p.append(hl)
                        else:
                            r_el = OxmlElement('w:r')
                            if first_rpr is not None:
                                import copy
                                r_el.append(copy.deepcopy(first_rpr))
                            t_el = OxmlElement('w:t')
                            t_el.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                            t_el.text = part
                            r_el.append(t_el)
                            p.append(r_el)


def _shade_cell(cell, hex_color: str) -> None:
    tc   = cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd  = OxmlElement('w:shd')
    shd.set(qn('w:val'),   'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'),  hex_color)
    tcPr.append(shd)


def _insert_photos_at_marker(doc: Document, marker: str, photos: list,
                              titles: list = None, fig_offset: int = 0,
                              show_figure_label: bool = True) -> None:
    """Find the marker paragraph and replace it with bordered photo tables."""
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

    valid = [(p, (titles[i] if titles and i < len(titles) else ''))
             for i, p in enumerate(photos)
             if p and os.path.exists(p)]

    for idx, (photo_path, title) in enumerate(valid, 1):
        if show_figure_label:
            cap_text = f'Figure {fig_offset + idx}'
            if title:
                cap_text += f':  {title}'
        else:
            cap_text = title  # title only, no "Figure N:" prefix

        # --- Bordered 2-row table: [image row] / [caption row] ---
        tbl = doc.add_table(rows=2, cols=1)
        tbl.style = 'Table Grid'

        # Row 0 — photo centred with padding
        img_cell = tbl.rows[0].cells[0]
        img_para = img_cell.paragraphs[0]
        img_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        img_para.paragraph_format.space_before = Pt(6)
        img_para.paragraph_format.space_after  = Pt(6)
        img_para.add_run().add_picture(photo_path, width=Inches(5.5))

        # Row 1 — caption, grey background
        cap_cell = tbl.rows[1].cells[0]
        _shade_cell(cap_cell, 'EBF5FB')
        cap_para = cap_cell.paragraphs[0]
        cap_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap_para.paragraph_format.space_before = Pt(4)
        cap_para.paragraph_format.space_after  = Pt(4)
        cap_run = cap_para.add_run(cap_text)
        cap_run.italic     = True
        cap_run.bold       = True
        cap_run.font.size  = Pt(9)

        # Add bookmark so (Photo No.-X) hyperlinks can navigate here
        if show_figure_label:
            _add_bookmark(cap_para, fig_offset + idx, f'figure_{fig_offset + idx}')

        # Move table to correct position (relationships stay in main doc ✓)
        tbl_elem = tbl._element
        parent.remove(tbl_elem)
        parent.insert(insert_pos, tbl_elem)
        insert_pos += 1

        # Small spacer paragraph between tables
        sp = doc.add_paragraph()
        sp.paragraph_format.space_before = Pt(0)
        sp.paragraph_format.space_after  = Pt(4)
        sp_elem = sp._element
        parent.remove(sp_elem)
        parent.insert(insert_pos, sp_elem)
        insert_pos += 1

        # Page break after every 2 photos (except the last)
        if idx % 2 == 0 and idx < len(valid):
            pb = doc.add_paragraph()
            br = OxmlElement('w:br')
            br.set(qn('w:type'), 'page')
            pb.add_run()._r.append(br)
            pb_elem = pb._element
            parent.remove(pb_elem)
            parent.insert(insert_pos, pb_elem)
            insert_pos += 1


def build_docx(report_json: dict) -> str:
    """Fill CASAD template with report_json and return saved file path."""
    doc = Document(TEMPLATE_PATH)
    _fill_placeholders(doc, report_json)

    raw_photos     = report_json.get('photos', [])
    raw_titles     = report_json.get('photo_titles', [])
    raw_categories = report_json.get('photo_categories', [])

    # Pad lists to same length
    n = len(raw_photos)
    raw_titles     = list(raw_titles)     + [''] * n
    raw_categories = list(raw_categories) + ['damage'] * n

    # Split into general and damage buckets, keeping titles aligned
    general_photos, general_titles = [], []
    damage_photos,  damage_titles  = [], []

    for path, title, cat in zip(raw_photos, raw_titles, raw_categories):
        if not path or not os.path.exists(path):
            continue
        if str(cat).lower() in ('damage', 'damaged'):
            damage_photos.append(path)
            damage_titles.append(title)
        else:
            general_photos.append(path)
            general_titles.append(title)

    print(f"BUILD_DOCX general photos: {general_photos}")
    print(f"BUILD_DOCX damage  photos: {damage_photos}")

    # Insert into Appendix A (general) — no figure numbering
    if general_photos:
        _insert_photos_at_marker(doc, '[[PHOTO_APPENDIX_A]]', general_photos, general_titles,
                                  fig_offset=0, show_figure_label=False)
    else:
        for para in doc.paragraphs:
            if para.text.strip() == '[[PHOTO_APPENDIX_A]]':
                para.runs[0].text = 'No general photographs submitted.'
                break

    # Insert into Appendix B (damage) — figure numbers always start at 1
    if damage_photos:
        _insert_photos_at_marker(doc, '[[PHOTO_APPENDIX_B]]', damage_photos, damage_titles,
                                  fig_offset=0)
    else:
        for para in doc.paragraphs:
            if para.text.strip() == '[[PHOTO_APPENDIX_B]]':
                para.runs[0].text = 'No damage photographs submitted.'
                break

    # Convert (Photo No.-X) text to clickable hyperlinks now that bookmarks exist
    _replace_photo_refs_with_hyperlinks(doc)

    river    = re.sub(r'[^\w\-]', '_', report_json.get('river_name', 'bridge'))
    road     = re.sub(r'[^\w\-]', '_', report_json.get('road_name',  'road'))
    date     = report_json.get('date_of_survey', 'report').replace('/', '-')
    out_path = os.path.join(OUTPUT_DIR, f'CASAD_{river}_{road}_{date}.docx')
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    doc.save(out_path)
    return out_path
