# report_gen_excel.py — Fill CASAD Excel template from report JSON
import os, re, io
from datetime import datetime
import openpyxl
from openpyxl.drawing.image import Image as XLImage

EXCEL_TEMPLATE_PATH = os.getenv('EXCEL_TEMPLATE_PATH', 'casad_excel_template.xlsx')
OUTPUT_DIR = os.getenv('OUTPUT_DIR', 'media')

DEFECT_ROW = {
    'cracks': 4, 'leaching': 5, 'honeycombing': 6, 'exposed_rebar': 7,
    'leakage': 8, 'spalling': 9, 'rust_marks': 10, 'shuttering': 11,
    'delamination': 12, 'vegetation': 13, 'any_other': 14
}

def _safe(d, key, default='-'):
    v = d.get(key)
    return str(v) if v else default

def _cell(ws, addr, value):
    """Write to a named cell address (e.g. 'C4'), unmerging if needed."""
    from openpyxl.utils import coordinate_to_tuple
    row, col = coordinate_to_tuple(addr)
    _safe_write(ws, row, col, value)

def _fill_title_page(wb, d):
    ws = wb['TITLE PAGE']
    _cell(ws, 'A2', f"CLIENT: {_safe(d, 'client_name', 'CASAD CONSULTANTS PVT. LTD.')}")
    _cell(ws, 'C4', _safe(d, 'project_name', 'Bridge Inspection Work'))
    _cell(ws, 'C5', f'"{_safe(d, "bridge_title", d.get("river_name", ""))}"')
    _cell(ws, 'A6', f"Project No.: {_safe(d, 'project_number', '-')}")
    from datetime import date
    _cell(ws, 'A10', 'R0')
    _cell(ws, 'B10', date.today())
    _cell(ws, 'D10', 'Preliminary Inspection Report')
    _cell(ws, 'E10', 'CASAD')

def _fill_appendix_a(wb, d):
    ws = wb['Appendix-A']
    mapping = {
        'C4':  d.get('bridge_title', d.get('river_name', '-')),  # Name of Bridge
        'C6':  d.get('river_name', '-'),
        'C7':  d.get('road_name', '-'),
        'C9':  f"{d.get('latitude','-')} , {d.get('longitude','-')}",
        'C11': d.get('division', d.get('circle', '-')),
        'C12': d.get('circle', '-'),
        'C15': d.get('no_of_spans', '-'),
        'C16': d.get('total_length', '-'),
        'C43': d.get('bridge_type', '-'),
        'C44': d.get('span_length', '-'),
        'C50': d.get('foundation_type', '-'),
        'C59': d.get('superstructure_type', '-'),
        'C64': d.get('bearing_type_detail', '-'),
        'C65': d.get('wearing_coat', '-'),
        'C66': d.get('railing_type', '-'),
        'C67': d.get('expansion_joint', '-'),
        'C93': d.get('date_of_survey', '-'),
    }
    for addr, val in mapping.items():
        _cell(ws, addr, val)

def _fill_appendix_b(wb, d):
    ws = wb['Appendix-B']
    # Correct row mapping verified against the actual R&B template structure
    fields = {
        # Section 1 — General identity (rows 4-12, blank in template)
        'C4':  d.get('bridge_title', d.get('river_name', '-')),
        'C6':  d.get('river_name', '-'),
        'C7':  d.get('road_name', '-'),
        'C8':  d.get('road_number', '-'),
        'C9':  f"{d.get('latitude','-')} , {d.get('longitude','-')}",
        'C10': f"{d.get('division','-')} / {d.get('circle','-')}",
        'C11': d.get('type_of_bridge', d.get('bridge_type', '-')),
        'C12': d.get('date_of_survey', '-'),
        # Section 4 — Approaches (rows 14-19)
        'C14': d.get('approach_settlement', '-'),
        'C15': d.get('approach_side_slopes', '-'),
        'C16': d.get('approach_erosion', '-'),
        'C17': d.get('approach_slab', '-'),
        'C18': d.get('approach_geometrics', '-'),
        'C19': d.get('approach_other', '-'),
        # Section 8 — Substructure cross-refs (rows 42-46)
        'C42': 'Refer Table  1 and 2',
        'C43': 'Refer Table  1 and 2',
        'C44': 'Refer Table  1 and 2',
        'C45': 'Refer Table  1 and 2',
        'C46': 'Refer Table  1 and 2',
        # Section 9.1.4 — Bearing pedestal/seismic arrestor cracks (row 52)
        'C52': d.get('sub_cracks', '-'),
        # Section 10 — Superstructure cross-refs (rows 59, 61-64)
        'C59': 'Refer Table  3 and 4',
        'C61': 'Refer Table  3 and 4',
        'C62': 'Refer Table  3 and 4',
        'C63': 'Refer Table  3 and 4',
        'C64': 'Refer Table  3 and 4',
    }
    for addr, val in fields.items():
        try:
            _cell(ws, addr, val or '-')
        except Exception:
            pass

def _col_letter(n):
    """Convert 1-based column index to Excel letter (A, B, ... Z, AA, ...)."""
    result = ''
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result

def _safe_write(ws, row: int, col: int, value):
    """Write to a cell, unmerging its range first if it is a MergedCell."""
    from openpyxl.cell.cell import MergedCell
    from openpyxl.utils import range_boundaries
    cell = ws.cell(row=row, column=col)
    if isinstance(cell, MergedCell):
        # Find the merge range that contains this cell and dissolve it
        for rng in list(ws.merged_cells.ranges):
            min_col, min_row, max_col, max_row = range_boundaries(str(rng))
            if min_row <= row <= max_row and min_col <= col <= max_col:
                ws.unmerge_cells(str(rng))
                break
    ws.cell(row=row, column=col).value = value

def _fill_defect_table(ws, elements: list, matrix: dict,
                        start_col: int, remarks_col: int):
    """Fill one defect table with pier/span IDs and observations.

    elements: list of pier or span ID strings
    matrix: {element_id: {defect_key: observation_string}}
    start_col: 1-based column index where element IDs start (row 3)
    remarks_col: 1-based column index of Remarks column (will be preserved)
    """
    if not elements:
        return

    # Clear existing header and data cells between start_col and remarks_col-1
    for col_i in range(start_col, remarks_col):
        _safe_write(ws, 3, col_i, None)
        for row in range(4, 15):
            _safe_write(ws, row, col_i, None)

    # Write new element IDs in row 3
    for i, elem_id in enumerate(elements):
        _safe_write(ws, 3, start_col + i, elem_id)

    # Write defect observations
    for defect_key, row_num in DEFECT_ROW.items():
        for i, elem_id in enumerate(elements):
            obs = (matrix.get(elem_id, {}) or {}).get(defect_key, 'Absent')
            _safe_write(ws, row_num, start_col + i, obs or 'Absent')

def _fill_defect_tables(wb, d):
    # Table 1: Sub-structure Side 1 (piers start col E=5, remarks col O=15)
    piers1 = d.get('sub_piers_side1') or []
    matrix1 = d.get('defect_sub1') or {}
    if piers1:
        _fill_defect_table(wb['Table 1'], piers1, matrix1, start_col=5, remarks_col=15)

    # Table 2: Sub-structure Side 2 (piers start col E=5, remarks col O=15)
    piers2 = d.get('sub_piers_side2') or []
    matrix2 = d.get('defect_sub2') or {}
    if piers2:
        _fill_defect_table(wb['Table 2'], piers2, matrix2, start_col=5, remarks_col=15)

    # Table 3: Super-structure Side 1 (spans start col E=5, remarks col S=19)
    spans1 = d.get('super_spans_side1') or []
    matrix3 = d.get('defect_super1') or {}
    if spans1:
        _fill_defect_table(wb['Table 3'], spans1, matrix3, start_col=5, remarks_col=19)

    # Table 4: Super-structure Side 2 (spans start col E=5, remarks col O=15)
    spans2 = d.get('super_spans_side2') or []
    matrix4 = d.get('defect_super2') or {}
    if spans2:
        _fill_defect_table(wb['Table 4'], spans2, matrix4, start_col=5, remarks_col=15)

def _fill_appendix_c(wb, d):
    """Insert photos into Appendix-C sheets."""
    photos     = d.get('photos', [])
    categories = d.get('photo_categories', [])
    titles     = d.get('photo_titles', [])

    sub_photos   = [(p, t) for p, c, t in zip(photos, categories, titles + [''] * len(photos))
                    if c not in ('damage', 'damaged')]
    super_photos = [(p, t) for p, c, t in zip(photos, categories, titles + [''] * len(photos))
                    if c in ('damage', 'damaged')]

    def _insert_photos(ws, photo_list):
        if not photo_list:
            return
        # Clear existing images
        ws._images.clear()
        row = 3
        for path, title in photo_list:
            if not path or not os.path.exists(path):
                continue
            try:
                from PIL import Image as PILImage
                with PILImage.open(path) as img:
                    img.load()
                    if img.mode in ('RGBA', 'P', 'LA'):
                        img = img.convert('RGB')
                    w, h = img.size
                    max_w, max_h = 400, 300
                    scale = min(max_w / w, max_h / h)
                    new_w, new_h = int(w * scale), int(h * scale)
                    img_resized = img.resize((new_w, new_h))
                    buf = io.BytesIO()
                    img_resized.save(buf, format='JPEG', quality=85)
                buf.seek(0)
                xl_img = XLImage(buf)
                xl_img.anchor = f'B{row}'
                ws.add_image(xl_img)
                # Write caption below photo
                ws.cell(row=row + 15, column=2, value=title or path)
                row += 20  # next photo slot
            except Exception as e:
                print(f"EXCEL PHOTO INSERT FAILED {path}: {e}")

    _insert_photos(wb['Appendix__C'], sub_photos)
    _insert_photos(wb['Appendix__C (2)'], super_photos)

def _fill_appendix_c_captions(wb, d):
    """Write photo caption list to Appendix-c sheet."""
    from openpyxl.cell.cell import MergedCell
    ws = wb['Appendix-c']
    photos     = d.get('photos', [])
    titles     = d.get('photo_titles', [])
    categories = d.get('photo_categories', [])

    # Clear existing content below row 1 (skip MergedCells)
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        for cell in row:
            if not isinstance(cell, MergedCell):
                cell.value = None

    row = 2
    _safe_write(ws, row, 2, 'SUB STRUCTURE')
    row += 1
    for i, (path, title, cat) in enumerate(zip(photos, titles, categories), 1):
        if cat not in ('damage', 'damaged'):
            _safe_write(ws, row, 2, f'{i}. {title}')
            row += 1

    row += 1
    _safe_write(ws, row, 2, 'SUPER STRUCTURE')
    row += 1
    for i, (path, title, cat) in enumerate(zip(photos, titles, categories), 1):
        if cat in ('damage', 'damaged'):
            _safe_write(ws, row, 2, f'{i}. {title}')
            row += 1

def build_excel(report_json: dict) -> str:
    """Fill CASAD Excel template with report_json and return saved file path."""
    wb = openpyxl.load_workbook(EXCEL_TEMPLATE_PATH)

    _fill_title_page(wb, report_json)
    _fill_appendix_a(wb, report_json)
    _fill_appendix_b(wb, report_json)
    _fill_defect_tables(wb, report_json)
    _fill_appendix_c_captions(wb, report_json)
    _fill_appendix_c(wb, report_json)

    # Prefer bridge_title for the filename; fall back to river_name
    name     = report_json.get('bridge_title') or report_json.get('river_name', 'bridge')
    road     = report_json.get('road_name', 'road')
    name     = re.sub(r'[^\w\-]', '_', name)
    road     = re.sub(r'[^\w\-]', '_', road)
    date_str = report_json.get('date_of_survey', 'report').replace('/', '-')
    out_path = os.path.join(OUTPUT_DIR, f'CASAD_{name}_{road}_{date_str}.xlsx')
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    wb.save(out_path)
    print(f"EXCEL REPORT SAVED: {out_path}")
    return out_path
