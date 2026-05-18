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
from datetime import date, datetime
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
    return str(v) if v is not None else default   # keeps 0 / False / '0'


def _fmt_date(val) -> str:
    """Return date as 'dd/mm/yyyy' string — never a datetime object.

    Writing a bare datetime via openpyxl produces an Excel serial number unless
    the target cell already carries a date number-format (which our templates
    don't guarantee).  Always use this for date cells.
    """
    if isinstance(val, (datetime, date)):
        return val.strftime('%d/%m/%Y')
    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
        try:
            return datetime.strptime(str(val), fmt).strftime('%d/%m/%Y')
        except (ValueError, TypeError):
            pass
    if val in (None, '', '-'):
        return '-'
    return str(val)


def _combine_fields(*parts, sep='\n') -> str:
    """Join non-empty / non-dash parts. Returns '-' when nothing to combine."""
    filtered = [str(p) for p in parts if p and str(p) not in ('-', '')]
    return sep.join(filtered) if filtered else '-'


def _rich_bold_labels(text) -> object:
    """Return a CellRichText where the portion before ':' on each line is bold.

    Used for multi-line fields like no_of_spans where each line has the form
    "Side Label: value". Returns None if no content is given (blank cell).
    Falls back to plain string if openpyxl rich-text is unavailable.
    """
    if not text or str(text).strip() in ('-', '', 'None'):
        return None
    try:
        from openpyxl.cell.rich_text import CellRichText, TextBlock
        from openpyxl.cell.text import InlineFont
    except ImportError:
        return str(text)   # older openpyxl — plain-string fallback

    bold = InlineFont(b=True)
    lines = str(text).split('\n')
    parts = CellRichText()
    for i, line in enumerate(lines):
        newline = '\n' if i < len(lines) - 1 else ''
        if ':' in line:
            label, rest = line.split(':', 1)
            parts.append(TextBlock(bold, label + ':'))
            parts.append(rest + newline)
        else:
            parts.append(line + newline)
    return parts


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
    """Fill one defect table sheet.

    ALWAYS clears the data region first to remove stale template data.
    Writes blank instead of 'Absent' for unobserved defects.
    """
    # Always clear — even when elements is empty
    clear_end = (start_col + len(elements)) if elements else remarks_col
    for col_i in range(start_col, clear_end):
        _safe_write(ws, 3, col_i, None)
        for row in range(4, 15):
            _safe_write(ws, row, col_i, None)

    if not elements:
        return

    for i, elem_id in enumerate(elements):
        _safe_write(ws, 3, start_col + i, elem_id)

    for defect_key, row_num in DEFECT_ROW.items():
        for i, elem_id in enumerate(elements):
            obs = (matrix.get(elem_id, {}) or {}).get(defect_key, '')
            if obs and str(obs).strip().lower() != 'absent':
                _safe_write(ws, row_num, start_col + i, obs)


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
    """Fill Appendix-A — AMC template row structure verified against
    casad_amc_template.xlsx (2026-05-18).  Row numbers match the R&B template
    exactly; earlier code had several off-by-one errors.
    """
    from openpyxl.cell.cell import MergedCell
    ws = wb['Appendix-A']

    # Step 1: Clear ALL variable data cells in column C (rows 4–104) to prevent
    # stale template data from bleeding through for cells not explicitly written.
    for r in range(4, 105):
        cell = ws.cell(row=r, column=3)
        if not isinstance(cell, MergedCell):
            cell.value = None

    lat = d.get('latitude', '-')
    lon = d.get('longitude', '-')
    mapping = {
        # ── Section 1 — General identity (rows 4-12) ─────────────────────────
        'C4':  d.get('bridge_title', d.get('river_name', '-')),
        'C5':  d.get('bridge_number', '-'),
        'C6':  d.get('river_name', '-'),
        'C7':  d.get('road_name', '-'),
        'C8':  d.get('road_number', '-'),
        'C9':  f"{lat}° , {lon}°",
        'C10': '-',          # BM / GTS level — rarely provided
        'C11': d.get('division', d.get('circle', '-')),
        'C12': d.get('circle', '-'),

        # Section 3 — Hydraulic Parameters section header
        'C20': d.get('hydraulic_parameters') or None,
        # Section 4 — Sub Soil Particulars section header
        'C33': d.get('subsoil_particulars') or None,

        # ── Section 2 — Details of Spans (rows 15-18) ────────────────────────
        # Row 14 = "Details of Spans:" section header — do NOT write here.
        # Row 2.1 label: "Number of Spans (Length, c/c of piers and width of
        # piers)" — one cell combining no_of_spans + span_length.
        # Sub-titles (the part before ':' on each line) are rendered in bold;
        # returns None (blank) when no data is provided.
        'C15': _rich_bold_labels(
            _combine_fields(d.get('no_of_spans', ''), d.get('span_length', ''))
        ),
        'C16': d.get('total_length', '-'),
        'C17': d.get('angle_of_crossing', '-'),
        'C18': d.get('bridge_level_type', d.get('type_of_bridge', '-')),

        # ── Section 5 — Design and Structural Data (rows 43-67) ──────────────
        # BUG FIXED: was C44/C45 — both shifted one row too low.
        # C43 = row-(b) "Type of Bridge (RCC solid slab, T-beam, etc.)"
        'C43': d.get('superstructure_type', d.get('bridge_type', '-')),
        # C44 = row-(c) "Span arrangement"
        'C44': d.get('span_arrangement', d.get('span_length', '-')),
        # C45 = row-(d) "Carriage width and footpath width"
        'C45': d.get('carriage_width', '-'),
        # C46 = row-(e) "Deck level (from F.R.L.)"
        'C46': d.get('deck_level', '-'),
        # C50 = row-(h) "Type of Foundations with salient details"
        'C50': d.get('foundation_type', '-'),
        # C52 = row-(i)(i) "Masonry, Mass Concrete, RCC"
        'C52': d.get('substructure_type', '-'),
        # C53 = row-(i)(ii) "Straight length of pier"
        'C53': d.get('pier_length', '-'),
        # BUG FIXED: was C61 (articulation row) — must be C59.
        # C59 = row-(j)(i) "Type of superstructure"
        'C59': d.get('superstructure_type', '-'),
        # C60 = row-(j)(ii) "Details of Prestressing"
        'C60': d.get('prestressing_details', 'As per approved Design'),
        # C64 = row-(m) "Type of bearings"
        'C64': d.get('bearing_type_detail') or None,
        'C65': d.get('wearing_coat') or None,
        'C66': d.get('railing_type') or None,
        'C67': d.get('expansion_joint') or None,

        # ── Section 7 — Other Data (rows 81-84) ──────────────────────────────
        'C81': _fmt_date(d.get('date_of_completion')) if d.get('date_of_completion') else '-',
        'C82': d.get('surface_utilities') or None,
        'C84': d.get('ls_sketch') or None,

        # ── Section 8-9 — Performance & survey date ───────────────────────────
        'C92': d.get('performance') or None,
        'C93': _fmt_date(d.get('date_of_survey')) if d.get('date_of_survey') else '-',
    }
    for addr, val in mapping.items():
        try:
            _cell(ws, addr, val)
        except Exception:
            pass


def _fill_appendix_b(wb, d):
    """Fill Appendix-B — AMC version. Pre-wiped to prevent stale template data."""
    from openpyxl.cell.cell import MergedCell
    ws = wb['Appendix-B']

    # Pre-wipe column C rows 4-75
    for r in range(4, 76):
        cell = ws.cell(row=r, column=3)
        if not isinstance(cell, MergedCell):
            cell.value = None

    lat = d.get('latitude', '-')
    lon = d.get('longitude', '-')
    div = d.get('division', '-')
    cir = d.get('circle', '-')

    def _v(key):
        v = d.get(key)
        return v if v and str(v).strip() not in ('-', '') else None

    fields = {
        'C4':  d.get('bridge_title', d.get('river_name', '-')),
        'C6':  d.get('river_name', '-'),
        'C7':  d.get('road_name', '-'),
        'C8':  d.get('road_number') or None,
        'C9':  f"{lat}° , {lon}°",
        'C10': div if div == cir else f"{div} / {cir}",
        'C11': d.get('type_of_bridge', d.get('bridge_type')) or None,
        'C12': d.get('date_of_survey', '-'),
        # Approaches (blank if not provided)
        'C14': _v('approach_settlement'),
        'C15': _v('approach_side_slopes'),
        'C16': _v('approach_erosion'),
        'C17': _v('approach_slab'),
        'C18': _v('approach_geometrics'),
        'C19': _v('approach_other'),
        # Substructure cross-refs
        'C42': 'Refer Table  1 and 2',
        'C43': 'Refer Table  1 and 2',
        'C44': 'Refer Table  1 and 2',
        'C45': 'Refer Table  1 and 2',
        'C46': 'Refer Table  1 and 2',
        # Bearing/pedestal cracks
        'C52': _v('sub_cracks'),
        # Superstructure cross-refs
        'C59': 'Refer Table  3 and 4',
        'C61': 'Refer Table  3 and 4',
        'C62': 'Refer Table  3 and 4',
        'C63': 'Refer Table  3 and 4',
        'C64': 'Refer Table  3 and 4',
    }
    for addr, val in fields.items():
        try:
            _cell(ws, addr, val)
        except Exception:
            pass


def _has_red_markers(path: str) -> bool:
    """Return True if the image already has prominent hand-drawn red circles (2% threshold)."""
    try:
        from PIL import Image as _PIL
        img = _PIL.open(path).convert('RGB').resize((120, 120))
        px  = list(img.getdata())
        red = sum(1 for r, g, b in px if r > 170 and g < 90 and b < 90)
        return red / len(px) > 0.02
    except Exception:
        return False


def _fill_appendix_c(wb, d):
    """Insert photos into the single Appendix-C- sheet (AMC format).

    Photos are inserted WITHOUT burning circles — editable ovals are injected
    as Excel AutoShapes after save.  Returns oval descriptor list.
    """
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, TwoCellAnchor

    ws = _find_sheet(wb, 'appendix-c')
    if ws is None:
        print("AMC PHOTO: Appendix-C sheet not found, skipping photos.")
        return []

    photos      = d.get('photos', [])
    titles      = d.get('photo_titles', [])
    categories  = d.get('photo_categories', [])
    coords_list = list(d.get('photo_coords', [])) + [None] * len(photos)

    # Pad lists to equal length
    max_len     = max(len(photos), len(titles), len(categories)) if any([photos, titles, categories]) else 0
    photos      = (photos      + [''] * max_len)[:max_len]
    titles      = (titles      + [''] * max_len)[:max_len]
    categories  = (categories  + [''] * max_len)[:max_len]
    coords_list = coords_list[:max_len]

    CAPTION_FILL   = PatternFill(patternType='solid', fgColor='FCE4D6')
    CAPTION_FONT   = Font(name='Times New Roman', size=11, bold=True)
    CAPTION_ALIGN  = Alignment(horizontal='center', vertical='center', wrap_text=True)
    _thin          = Side(style='thin', color='000000')
    CAPTION_BORDER = Border(top=_thin, left=_thin, right=_thin, bottom=_thin)

    ws._images.clear()
    for rng in list(ws.merged_cells.ranges):
        r = str(rng)
        if 'B2' not in r and 'A1' not in r:
            try:
                ws.unmerge_cells(r)
            except Exception:
                pass

    ovals     = []
    shape_ctr = 200   # start higher than R&B module to avoid ID collision

    row = 3
    for path, title, cat, coords in zip(photos, titles, categories, coords_list):
        if not path or not os.path.exists(path):
            continue
        try:
            from PIL import Image as PILImage
            with PILImage.open(path) as img:
                img.load()
                if img.mode in ('RGBA', 'P', 'LA'):
                    img = img.convert('RGB')
                # No burned-in circle — editable oval injected after save
                w, h    = img.size
                scale   = min(600 / w, 420 / h)
                new_w   = int(w * scale)
                new_h   = int(h * scale)
                buf     = io.BytesIO()
                img.resize((new_w, new_h), PILImage.LANCZOS).save(buf, format='JPEG', quality=90)
            buf.seek(0)

            ph_from_row = row - 1
            ph_to_row   = row + 19

            xl_img        = XLImage(buf)
            xl_img.width  = new_w
            xl_img.height = new_h
            anchor        = TwoCellAnchor()
            anchor._from  = AnchorMarker(col=0, row=ph_from_row, colOff=0, rowOff=0)
            anchor.to     = AnchorMarker(col=8, row=ph_to_row,   colOff=0, rowOff=0)
            anchor.editAs = 'oneCell'
            xl_img.anchor = anchor
            ws.add_image(xl_img)

            # Schedule editable oval if defect coords available
            if coords and not _has_red_markers(path):
                x_pct, y_pct = coords
                span_cols = 8
                span_rows = ph_to_row - ph_from_row   # 20
                r_cols = max(1, int(span_cols * 0.07))
                r_rows = max(1, int(span_rows * 0.07))
                oval_fc = max(0, int(x_pct * span_cols) - r_cols)
                oval_tc = min(span_cols, int(x_pct * span_cols) + r_cols)
                oval_fr = ph_from_row + max(0, int(y_pct * span_rows) - r_rows)
                oval_tr = ph_from_row + min(span_rows, int(y_pct * span_rows) + r_rows)
                shape_ctr += 1
                ovals.append((ws.title, oval_fc, oval_fr, oval_tc, oval_tr, shape_ctr))

            cap_row = ph_to_row + 2
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

    return ovals


def build_excel_amc(report_json: dict) -> str:
    """Fill CASAD AMC Excel template with report_json and return saved file path."""
    wb = openpyxl.load_workbook(AMC_TEMPLATE_PATH)

    _fill_title_page(wb, report_json)
    _fill_appendix_a(wb, report_json)
    _fill_appendix_b(wb, report_json)
    _fill_defect_tables(wb, report_json)
    ovals = _fill_appendix_c(wb, report_json)

    bridge   = re.sub(r'[^\w\-]', '_', report_json.get('bridge_title', report_json.get('river_name', 'bridge')))
    date_str = report_json.get('date_of_survey', 'report').replace('/', '-')
    out_path = os.path.join(OUTPUT_DIR, f'CASAD_AMC_{bridge}_{date_str}.xlsx')
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    wb.save(out_path)
    print(f"AMC EXCEL REPORT SAVED: {out_path}")

    # Inject editable oval shapes after save
    if ovals:
        try:
            from report_gen_excel import _inject_oval_shapes
            _inject_oval_shapes(out_path, ovals)
        except Exception as e:
            print(f"AMC OVAL INJECT FAILED: {e}")

    return out_path
