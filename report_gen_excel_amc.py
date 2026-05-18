# report_gen_excel_amc.py — Fill CASAD AMC Excel template from report JSON
#
# AMC template structure (Nathalal Zaghda style):
#   - TITLE PAGE, DISCLAIMR, Appendix-A, Appendix-B, Appendix-B 21-22, Appendix-C-
#   - Defect sheets named: "[BRIDGE] SUB - 1", "[BRIDGE] SUB - 2",
#                          "[BRIDGE] SUPER - 1", "[BRIDGE] SUPER - 2"
#   - All defect tables: row 3 = pier/span IDs starting col 5 (E),
#                        rows 4-14 = defect observations (a-k)
#   - Remarks column detected dynamically from row 3
#   - Single photo sheet: "Appendix-C-"

import os, re, io
from datetime import date
import openpyxl
from openpyxl.drawing.image import Image as XLImage

AMC_TEMPLATE_PATH = os.getenv('AMC_TEMPLATE_PATH', 'casad_amc_template.xlsx')
OUTPUT_DIR = os.getenv('OUTPUT_DIR', 'media')

DEFECT_ROW = {
    'cracks': 4, 'leaching': 5, 'honeycombing': 6, 'exposed_rebar': 7,
    'leakage': 8, 'spalling': 9, 'rust_marks': 10, 'shuttering': 11,
    'delamination': 12, 'vegetation': 13, 'any_other': 14
}


def _safe(d, key, default='-'):
    v = d.get(key)
    return str(v) if v else default


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
        for rng in list(ws.merged_cells.ranges):
            min_col, min_row, max_col, max_row = range_boundaries(str(rng))
            if min_row <= row <= max_row and min_col <= col <= max_col:
                ws.unmerge_cells(str(rng))
                break
    ws.cell(row=row, column=col).value = value

def _cell(ws, addr, value):
    """Write to a named cell address (e.g. 'C4'), unmerging if needed."""
    from openpyxl.utils import coordinate_to_tuple
    row, col = coordinate_to_tuple(addr)
    _safe_write(ws, row, col, value)


def _find_sheet(wb, keyword):
    """Find the first sheet whose name contains keyword (case-insensitive)."""
    kw = keyword.lower()
    for name in wb.sheetnames:
        if kw in name.lower():
            return wb[name]
    return None


def _detect_remarks_col(ws):
    """Scan row 3 to find the 1-based column index of the Remarks column."""
    for cell in ws[3]:
        if cell.value and 'remark' in str(cell.value).lower():
            return cell.column
    # Fallback: one past the last non-empty cell in row 3
    last = 4
    for cell in ws[3]:
        if cell.value is not None and cell.column >= 5:
            last = cell.column
    return last + 1


def _fill_defect_table(ws, elements: list, matrix: dict, start_col: int, remarks_col: int):
    """Fill one defect table sheet with pier/span IDs and observations.

    elements   : list of pier or span ID strings (e.g. ['A1','P1','P2',...])
    matrix     : {element_id: {defect_key: observation_string}}
    start_col  : 1-based column index where element IDs start (row 3)
    remarks_col: 1-based column index of the Remarks column (preserved, not overwritten)
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

    # Write defect observations rows 4-14
    for defect_key, row_num in DEFECT_ROW.items():
        for i, elem_id in enumerate(elements):
            obs = (matrix.get(elem_id, {}) or {}).get(defect_key, 'Absent')
            _safe_write(ws, row_num, start_col + i, obs or 'Absent')


def _fill_defect_tables(wb, d):
    """Find and fill all four defect sheets in the AMC template."""
    START_COL = 5  # column E — same in both R&B and AMC templates

    # Sub-structure Side 1
    ws1 = _find_sheet(wb, 'sub - 1')
    if ws1:
        piers1  = d.get('sub_piers_side1') or []
        matrix1 = d.get('defect_sub1') or {}
        if piers1:
            remarks_col = _detect_remarks_col(ws1)
            _fill_defect_table(ws1, piers1, matrix1, START_COL, remarks_col)

    # Sub-structure Side 2
    ws2 = _find_sheet(wb, 'sub - 2')
    if ws2:
        piers2  = d.get('sub_piers_side2') or []
        matrix2 = d.get('defect_sub2') or {}
        if piers2:
            remarks_col = _detect_remarks_col(ws2)
            _fill_defect_table(ws2, piers2, matrix2, START_COL, remarks_col)

    # Super-structure Side 1
    ws3 = _find_sheet(wb, 'super - 1')
    if ws3:
        spans1  = d.get('super_spans_side1') or []
        matrix3 = d.get('defect_super1') or {}
        if spans1:
            remarks_col = _detect_remarks_col(ws3)
            _fill_defect_table(ws3, spans1, matrix3, START_COL, remarks_col)

    # Super-structure Side 2
    ws4 = _find_sheet(wb, 'super - 2')
    if ws4:
        spans2  = d.get('super_spans_side2') or []
        matrix4 = d.get('defect_super2') or {}
        if spans2:
            remarks_col = _detect_remarks_col(ws4)
            _fill_defect_table(ws4, spans2, matrix4, START_COL, remarks_col)


def _fill_title_page(wb, d):
    ws = wb['TITLE PAGE']
    _cell(ws, 'A2', f"CLIENT: {_safe(d, 'client_name', 'AHMEDABAD MUNICIPAL CORPORATION')}")
    _cell(ws, 'C3', _safe(d, 'project_name', 'Bridge Inspection Work Ahmedabad City'))
    _cell(ws, 'C4', f'"{_safe(d, "bridge_title_full", _safe(d, "bridge_title", d.get("river_name", "")))}"')
    _cell(ws, 'A5', f"Project No.: {_safe(d, 'project_number', '-')}")
    _cell(ws, 'A8', 'R0')
    _cell(ws, 'B8', date.today())
    _cell(ws, 'D8', 'Preliminary Inspection Report')
    _cell(ws, 'E8', 'CASAD')


def _fill_appendix_a(wb, d):
    """Fill Appendix-A — same structure as R&B (4 cols, same row layout)."""
    ws = wb['Appendix-A']
    mapping = {
        'C4':  d.get('bridge_title', d.get('river_name', '-')),
        'C7':  d.get('road_name', '-'),
        'C8':  d.get('road_number', '-'),
        'C9':  f"{d.get('latitude', '-')} , {d.get('longitude', '-')}",
        'C11': d.get('division', '-'),
        'C12': d.get('circle', '-'),
        'C14': d.get('no_of_spans', '-'),
        'C15': d.get('total_length', '-'),
        'C44': d.get('bridge_type', '-'),
        'C45': d.get('span_length', '-'),
        'C61': d.get('superstructure_type', '-'),
    }
    for addr, val in mapping.items():
        try:
            _cell(ws, addr, val or '-')
        except Exception:
            pass


def _fill_appendix_b(wb, d):
    """Fill Appendix-B — AMC version has 3 columns (Sr.No, Details, Observations)."""
    ws = wb['Appendix-B']
    # Column C = Observations
    fields = {
        'C4':  d.get('bridge_title', d.get('river_name', '-')),
        'C7':  d.get('road_name', '-'),
        'C9':  f"{d.get('latitude', '-')} , {d.get('longitude', '-')}",
        'C10': d.get('division', '-') if d.get('division') == d.get('circle') else f"{d.get('division','-')} / {d.get('circle','-')}",
        'C11': d.get('type_of_bridge', d.get('bridge_type', '-')),
        'C12': d.get('date_of_survey', '-'),
        # Approaches
        'C16': d.get('approach_settlement', '-'),
        'C17': d.get('approach_side_slopes', '-'),
        'C18': d.get('approach_erosion', '-'),
        'C19': d.get('approach_slab', '-'),
        'C20': d.get('approach_geometrics', '-'),
        'C21': d.get('approach_other', '-'),
        # Substructure
        'C43': d.get('sub_cracks', '-'),
        'C44': d.get('sub_other', '-'),
        # Bearings
        'C48': d.get('bearing_condition', '-'),
        # Superstructure
        'C55': d.get('ss_cracks', '-'),
        'C56': d.get('ss_spalling', '-'),
        'C57': d.get('ss_exposed_rebar', '-'),
    }
    for addr, val in fields.items():
        try:
            _cell(ws, addr, val or '-')
        except Exception:
            pass


def _fill_appendix_c(wb, d):
    """Insert photos into the single Appendix-C- sheet (AMC format)."""
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, TwoCellAnchor

    ws = _find_sheet(wb, 'appendix-c')
    if ws is None:
        print("AMC PHOTO: Appendix-C sheet not found, skipping photos.")
        return

    photos     = d.get('photos', [])
    titles     = d.get('photo_titles', [])
    categories = d.get('photo_categories', [])

    # Pad lists to equal length
    max_len    = max(len(photos), len(titles), len(categories)) if any([photos, titles, categories]) else 0
    photos     = (photos     + [''] * max_len)[:max_len]
    titles     = (titles     + [''] * max_len)[:max_len]
    categories = (categories + [''] * max_len)[:max_len]

    # Caption style matching original
    CAPTION_FILL   = PatternFill(patternType='solid', fgColor='FCE4D6')
    CAPTION_FONT   = Font(name='Times New Roman', size=11, bold=True)
    CAPTION_ALIGN  = Alignment(horizontal='center', vertical='center', wrap_text=True)
    _thin          = Side(style='thin', color='000000')
    CAPTION_BORDER = Border(top=_thin, left=_thin, right=_thin, bottom=_thin)

    ws._images.clear()
    # Clear existing caption merges (keep header row merges)
    for rng in list(ws.merged_cells.ranges):
        r = str(rng)
        if 'B2' not in r and 'A1' not in r:
            try:
                ws.unmerge_cells(r)
            except Exception:
                pass

    row = 3
    for path, title, cat in zip(photos, titles, categories):
        if not path or not os.path.exists(path):
            continue
        try:
            from PIL import Image as PILImage
            with PILImage.open(path) as img:
                img.load()
                if img.mode in ('RGBA', 'P', 'LA'):
                    img = img.convert('RGB')
                w, h = img.size
                max_w, max_h = 600, 420
                scale       = min(max_w / w, max_h / h)
                new_w       = int(w * scale)
                new_h       = int(h * scale)
                img_res     = img.resize((new_w, new_h), PILImage.LANCZOS)
                buf = io.BytesIO()
                img_res.save(buf, format='JPEG', quality=90)
            buf.seek(0)

            xl_img        = XLImage(buf)
            xl_img.width  = new_w
            xl_img.height = new_h

            from_row = row - 1
            to_row   = row + 19

            anchor        = TwoCellAnchor()
            anchor._from  = AnchorMarker(col=0, row=from_row, colOff=0, rowOff=0)
            anchor.to     = AnchorMarker(col=8, row=to_row,   colOff=0, rowOff=0)
            anchor.editAs = 'oneCell'
            xl_img.anchor = anchor
            ws.add_image(xl_img)

            # Caption row
            cap_row = to_row + 2
            try:
                ws.merge_cells(start_row=cap_row, start_column=1,
                               end_row=cap_row, end_column=8)
            except Exception:
                pass
            cap_cell           = ws.cell(row=cap_row, column=1, value=title or path)
            cap_cell.fill      = CAPTION_FILL
            cap_cell.font      = CAPTION_FONT
            cap_cell.alignment = CAPTION_ALIGN
            cap_cell.border    = CAPTION_BORDER
            ws.row_dimensions[cap_row].height = 28

            row = cap_row + 3

        except Exception as e:
            print(f"AMC PHOTO INSERT FAILED {path}: {e}")


def build_excel_amc(report_json: dict) -> str:
    """Fill CASAD AMC Excel template with report_json and return saved file path."""
    wb = openpyxl.load_workbook(AMC_TEMPLATE_PATH)

    _fill_title_page(wb, report_json)
    _fill_appendix_a(wb, report_json)
    _fill_appendix_b(wb, report_json)
    _fill_defect_tables(wb, report_json)
    _fill_appendix_c(wb, report_json)

    bridge   = re.sub(r'[^\w\-]', '_', report_json.get('bridge_title', report_json.get('river_name', 'bridge')))
    date_str = report_json.get('date_of_survey', 'report').replace('/', '-')
    out_path = os.path.join(OUTPUT_DIR, f'CASAD_AMC_{bridge}_{date_str}.xlsx')
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    wb.save(out_path)
    print(f"AMC EXCEL REPORT SAVED: {out_path}")
    return out_path
