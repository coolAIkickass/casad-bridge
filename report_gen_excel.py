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
    _cell(ws, 'C5', f'"{_safe(d, "bridge_title_full", _safe(d, "bridge_title", d.get("river_name", "")))}"')
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
        'C10': d.get('division', '-') if d.get('division') == d.get('circle') else f"{d.get('division','-')} / {d.get('circle','-')}",
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
    # Table 1: Sub-structure Side 1 (piers start col E=5, remarks col P=16)
    piers1 = d.get('sub_piers_side1') or []
    matrix1 = d.get('defect_sub1') or {}
    if piers1:
        _fill_defect_table(wb['Table 1'], piers1, matrix1, start_col=5, remarks_col=16)

    # Table 2: Sub-structure Side 2 (piers start col E=5, remarks col O=15)
    piers2 = d.get('sub_piers_side2') or []
    matrix2 = d.get('defect_sub2') or {}
    if piers2:
        _fill_defect_table(wb['Table 2'], piers2, matrix2, start_col=5, remarks_col=15)

    # Table 3: Super-structure Side 1 — special column layout
    # The template has the Railway span at col E (5), then cols F & G are merged/empty,
    # then road spans start at col H (8).  Remarks at col S (19).
    spans1  = d.get('super_spans_side1') or []
    matrix3 = d.get('defect_super1') or {}
    ws3     = wb['Table 3']
    if spans1:
        # Clear the full data area (cols E–R, rows 3–14) before writing
        for col_i in range(5, 19):
            _safe_write(ws3, 3, col_i, None)
            for row in range(4, 15):
                _safe_write(ws3, row, col_i, None)
        # Write the first span (Railway span) at col E (5)
        rly_span = spans1[0]
        _safe_write(ws3, 3, 5, rly_span)
        for defect_key, row_num in DEFECT_ROW.items():
            obs = (matrix3.get(rly_span, {}) or {}).get(defect_key, 'Absent')
            _safe_write(ws3, row_num, 5, obs or 'Absent')
        # Write remaining spans sequentially starting at col H (8) — skipping F, G
        for i, span_id in enumerate(spans1[1:]):
            col = 8 + i  # H=8, I=9, J=10, …
            _safe_write(ws3, 3, col, span_id)
            for defect_key, row_num in DEFECT_ROW.items():
                obs = (matrix3.get(span_id, {}) or {}).get(defect_key, 'Absent')
                _safe_write(ws3, row_num, col, obs or 'Absent')

    # Table 4: Super-structure Side 2 (spans start col E=5, remarks col O=15)
    spans2 = d.get('super_spans_side2') or []
    matrix4 = d.get('defect_super2') or {}
    if spans2:
        _fill_defect_table(wb['Table 4'], spans2, matrix4, start_col=5, remarks_col=15)

def _has_red_markers(path: str) -> bool:
    """Return True if the image already has significant red circle/mark content."""
    try:
        from PIL import Image as _PIL
        img = _PIL.open(path).convert('RGB').resize((120, 120))
        px = list(img.getdata())
        red = sum(1 for r, g, b in px if r > 170 and g < 90 and b < 90)
        return red / len(px) > 0.003   # 0.3 % threshold
    except Exception:
        return False


def _draw_red_circle(img, x_pct: float, y_pct: float):
    """Draw a red circle on a PIL image at the relative defect position."""
    from PIL import ImageDraw
    w, h   = img.size
    cx     = int(x_pct * w)
    cy     = int(y_pct * h)
    r      = max(18, int(min(w, h) * 0.07))   # 7% of smaller dimension
    width  = max(3,  int(min(w, h) * 0.012))  # ~1.2% stroke
    draw   = ImageDraw.Draw(img)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline='red', width=width)
    return img


def _fill_appendix_c(wb, d):
    """Insert photos into Appendix-C sheets, matching original template style."""
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, TwoCellAnchor
    from openpyxl.utils.units import pixels_to_points

    photos      = d.get('photos', [])
    categories  = d.get('photo_categories', [])
    titles      = d.get('photo_titles', [])
    coords_list = d.get('photo_coords', [])

    # Pad coords_list so it matches photos length
    coords_list = list(coords_list) + [None] * len(photos)

    sub_photos   = [(p, t, c) for p, cat, t, c in
                    zip(photos, categories,
                        titles + [''] * len(photos),
                        coords_list)
                    if cat not in ('damage', 'damaged')]
    super_photos = [(p, t, c) for p, cat, t, c in
                    zip(photos, categories,
                        titles + [''] * len(photos),
                        coords_list)
                    if cat in ('damage', 'damaged')]

    # Caption style matching original: light peach fill, Times New Roman 11pt bold, centred, thin border
    CAPTION_FILL   = PatternFill(patternType='solid', fgColor='FCE4D6')   # accent2 ~60% tint
    CAPTION_FONT   = Font(name='Times New Roman', size=11, bold=True)
    CAPTION_ALIGN  = Alignment(horizontal='center', vertical='center', wrap_text=True)
    _thin          = Side(style='thin', color='000000')
    CAPTION_BORDER = Border(top=_thin, left=_thin, right=_thin, bottom=_thin)

    def _insert_photos(ws, photo_list):
        if not photo_list:
            return
        ws._images.clear()

        # Pre-clear any existing caption merged ranges (B/A col, rows > 2)
        for rng in list(ws.merged_cells.ranges):
            if str(rng) != 'B2:H2':
                try:
                    ws.unmerge_cells(str(rng))
                except Exception:
                    pass

        row = 3  # first photo starts at row 3
        for path, title, coords in photo_list:
            if not path or not os.path.exists(path):
                continue
            try:
                from PIL import Image as PILImage
                with PILImage.open(path) as img:
                    img.load()
                    if img.mode in ('RGBA', 'P', 'LA'):
                        img = img.convert('RGB')
                    # Draw red circle if AI detected coords AND photo has no pre-drawn circle
                    if coords and not _has_red_markers(path):
                        img = _draw_red_circle(img, coords[0], coords[1])
                    w, h = img.size
                    # Scale to fill the sheet width (~600px wide × 420px tall max)
                    max_w, max_h = 600, 420
                    scale   = min(max_w / w, max_h / h)
                    new_w   = int(w * scale)
                    new_h   = int(h * scale)
                    img_res = img.resize((new_w, new_h), PILImage.LANCZOS)
                    buf = io.BytesIO()
                    img_res.save(buf, format='JPEG', quality=90)
                buf.seek(0)

                # Two-cell anchor: from A{row} to I{row+photo_rows-1}
                # 1 px ≈ 9525 EMU; row height default 15pt → 20 rows ≈ image height
                EMU_PER_PX = 9525
                from_row   = row - 1       # 0-based
                to_row     = row + 19      # ~20 rows for photo
                xl_img = XLImage(buf)
                xl_img.width  = new_w
                xl_img.height = new_h

                # Build TwoCellAnchor manually so image fills A–H
                anchor = TwoCellAnchor()
                anchor._from = AnchorMarker(col=0, row=from_row, colOff=0, rowOff=0)   # col A
                anchor.to    = AnchorMarker(col=8, row=to_row,   colOff=0, rowOff=0)   # col I
                anchor.editAs = 'oneCell'
                xl_img.anchor = anchor
                ws.add_image(xl_img)

                # Caption row: 2 rows below photo block, span A:H, styled
                cap_row = to_row + 2   # 1-based
                try:
                    ws.merge_cells(start_row=cap_row, start_column=1,
                                   end_row=cap_row, end_column=8)
                except Exception:
                    pass
                cap_cell               = ws.cell(row=cap_row, column=1, value=title or path)
                cap_cell.fill          = CAPTION_FILL
                cap_cell.font          = CAPTION_FONT
                cap_cell.alignment     = CAPTION_ALIGN
                cap_cell.border        = CAPTION_BORDER
                # Set row height so caption text is visible
                ws.row_dimensions[cap_row].height = 28

                row = cap_row + 3   # 2 blank rows gap before next photo

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
