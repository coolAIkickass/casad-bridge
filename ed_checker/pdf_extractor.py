"""
Extract structured data from an AutoCAD engineering drawing PDF.
Step 1: pdfplumber text extraction (title block, note labels, grades).
Step 2: PyMuPDF → image → Claude vision API (schedule tables, TABLE-1, notes).
"""
import io
import os
import re
import json
import base64
import logging
import pdfplumber

log = logging.getLogger(__name__)
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')


def _norm_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
# Use ED_MODEL env var to override: 'haiku' (cheap, for testing) or 'sonnet' (production)
_model_alias = os.environ.get('ED_MODEL', 'haiku').lower()
EXTRACT_MODEL = 'claude-haiku-4-5-20251001' if _model_alias == 'haiku' else 'claude-sonnet-4-6'
REVIEW_MODEL  = 'claude-haiku-4-5-20251001'  # always Haiku — review call doesn't need Sonnet

EXTRACTION_PROMPT = """You are analyzing a CASAD AutoCAD engineering drawing for a bridge structure (Pile-Pilecap-Pier foundation).

Extract ALL of the following as precisely as possible:

1. SCHEDULE OF REINFORCEMENT — multiple sections exist (Pilecap, Pile per pile, Pier).

   First, identify the bounding box of EACH COMPONENT'S ENTIRE SCHEDULE SECTION (the full table block
   for that component, including its header row). Return these in "schedule_section_bboxes".
   Bboxes are percentages of the full image: {"x": <left%>, "y": <top%>, "w": <width%>, "h": <height%>}.
   Example: {"pilecap": {"x": 63, "y": 22, "w": 34, "h": 14}, "pile": {...}, "pier": {...}}

   Then for EACH row in any schedule table, extract:
   {
     "bar_mark": "a",
     "component": "pilecap" | "pile" | "pier",
     "reinforcement_text": "25Φ – 42 NOS.",
     "bar_dia_mm": 25,
     "spacing_mm": null,
     "count_text": "42",
     "count": 42,
     "length_m": 13.425,
     "total_length_m": 563.85,
     "unit_wt_kg_m": 3.857,
     "total_wt_kg": 2174.77
   }

   COLUMN MAPPING RULES — read these carefully:
   - bar_dia_mm: the φ / DIA column — always a small integer (8, 10, 12, 16, 20, 25, 32). Do NOT confuse with length or spacing.
   - spacing_mm: the c/c spacing column — null for longitudinal bars (shown as "-"). For rings/stirrups this is the pitch in mm.
   - length_m: the individual bar LENGTH in metres — a decimal number like 9.160, 4.049, 13.425. Do NOT use spacing or weight here.
   - count: the total number of bars. The count column may contain multiplication expressions.
     ALWAYS use the value AFTER the "=" sign as the count — it is the total.
     Examples: "4×13 = 52" → count=52, count_text="4×13 = 52"
               "21 * 4 = 84" → count=84, count_text="21 * 4 = 84"
               "6 × 21 = 126" → count=126, count_text="6 × 21 = 126"
     Both "×" and "*" are multiplication symbols. The number BEFORE "=" is never the count.
   - If the same bar mark appears in two rows (e.g. two 'y' rows for different ring zones), return BOTH rows separately with the same bar_mark and component.

   BAR MARK COMPLETENESS — extract ALL rows, including bars with suffixed marks:
   k1, j1, i1, f1, y1, x1, z — these small labels appear at the end of each component section.
   Do not stop after the first few rows. Scan the full table to the bottom of each section.
   No per-row bbox is needed — section bboxes handle highlighting.

2. TITLE BLOCK — extract:
   {
     "project_name": "...",
     "drawing_number": "IND/RAJ/PPP-01A",
     "revision": "R2",
     "title": "DETAILS OF PILE, PILECAP AND PIER",
     "spans": "30.0M - 30.0M",
     "width": "16.6M",
     "pier_range": "P3 TO P7",
     "drawn_by": "H.R.ROHIT",
     "design_by": "M.M.MODY",
     "approved_by": "J.B.GANDHI",
     "date": "21-04-2026",
     "scale": "AS SHOWN",
     "bbox": {"x": 63.0, "y": 77.0, "w": 35.0, "h": 21.0}
   }

3. NOTES — extract these specific values if present:
   {
     "pile_length_m": 12.0,
     "pile_fixity_m": 7.9,
     "pile_dia_m": 1.2,
     "max_pile_load_t": null,
     "bore_log_ref": null,
     "concrete_pile": "M40",
     "concrete_pilecap": "M35",
     "concrete_pier": "M35",
     "steel_grade": "Fe550",
     "lap_length_concrete_grade": "M35",
     "bbox": {"x": 40.0, "y": 68.0, "w": 22.0, "h": 10.0}
   }
   IMPORTANT: If a single concrete grade is stated without a component qualifier
   (e.g. "Concrete Mix M35", "All M35", or just "M35" in the notes), set
   concrete_pile, concrete_pilecap AND concrete_pier all to that grade.

4. TABLE-1 (levels table, if visible) — for each pier row:
   {
     "pier_id": "P7",
     "top_pier_cap_m": 98.5,
     "top_pier_m": null,
     "top_pilecap_m": null,
     "bottom_pilecap_m": null,
     "ground_level_m": null,
     "bbox": {"x": 82.0, "y": 2.0, "w": 16.0, "h": 3.0}
   }

Return ONLY valid JSON with this structure (no markdown, no extra text):
{
  "schedule": [...],
  "schedule_section_bboxes": {
    "pilecap": {"x": 63, "y": 22, "w": 34, "h": 14},
    "pile":    {"x": 63, "y": 36, "w": 34, "h": 12},
    "pier":    {"x": 63, "y": 48, "w": 34, "h": 22}
  },
  "title_block": {...},
  "notes": {...},
  "table_1": [...]
}
Use null for any value not found or not legible.
"""

REVIEW_PROMPT = """You are doing a quality review of a CASAD bridge engineering drawing (Pile-Pilecap-Pier foundation type).

Scan the ENTIRE drawing image carefully and return findings for each check below.
For every finding include a bbox: {"x": <left%>, "y": <top%>, "w": <width%>, "h": <height%>} as % of image dimensions.

CHECK 1 — Required views/sections
For each view listed, report whether it is present and what scale is shown on it:
SECTION A-A FOR PILE, SECTION Z-Z (PILE),
SECTION A-A FOR PILECAP & PIER, SECTION B-B FOR PILECAP & PIER,
PLAN OF PILECAP, REINFORCEMENT PLAN OF PILECAP,
DETAIL A (ring details), TABLE-1, LAP LENGTH TABLE, SCHEDULE OF REINFORCEMENT

CHECK 2 — Notes completeness
Check whether the NOTES section contains each of these items and extract its value.
Use EXACTLY these item values in the "item" field — no variations:
  "pile_length", "pile_fixity", "pile_diameter", "max_pile_load",
  "bore_log_ref", "concrete_pile", "concrete_pilecap", "concrete_pier",
  "steel_grade", "pile_projection", "irc_code_ref"

CHECK 3 — Label & annotation quality
Scan all visible text labels and annotations for GENUINE ERRORS ONLY:
- Spelling errors: only flag if you can clearly read the incorrect letters AND the correct
  spelling is unambiguous. Do NOT flag a word that looks correct at normal reading speed.
  Do NOT flag CONTRACTOR, REINFORCEMENT, FOUNDATION, or other common engineering words
  unless you can see the misspelling character-by-character with certainty.
- Bar mark labels in section views that don't match the schedule
- Scale labels that are clearly inconsistent with each other
- Any label that clearly points to the wrong component
- Text that is obviously truncated or cut off
- Concrete grade mismatch: if a section view or plan view has a concrete grade annotation
  (e.g. "M35", "M50") directly labelled on the structural drawing, cross-check it against
  the grade stated in the NOTES section of the same drawing. If they differ, flag it.
  Example: notes say M50 for pilecap/pier but SECTION A-A FOR PILECAP & PIER shows "M35".

STRICT EXCLUSIONS — do NOT flag any of the following:
- "SECTION A-A FOR PILE" vs "SECTION A-A FOR PILECAP & PIER" — these are two different sections with
  correct distinct names; the "FOR PILE" vs "FOR PILECAP & PIER" suffix is intentional, not inconsistent.
- "BUNDLE BARS" — this is the correct engineering term; do not flag it as a grammar issue.
- Dimension values, units, or notation styles that follow standard structural drawing conventions.
- Text density, font size, or legibility observations about schedule tables (e.g. "text appears cut off
  in schedule", "bar marks are small and difficult to cross-reference"). These are style observations,
  not annotation errors — do NOT include them in label_issues.
Only report items you are CERTAIN are incorrect. When in doubt, omit.

CHECK 4 — Dimension completeness (MISSING DIMENSIONS ONLY)
Only flag a dimension type if it is COMPLETELY ABSENT from the entire drawing.
Flag ONLY if you cannot find the dimension anywhere:
- Pile cap overall length (along traffic)
- Pile cap overall width (across traffic)
- Pile cap depth/thickness
- Pile spacing (centre-to-centre)
- Pier cross-section dimensions

CRITICAL RULES for CHECK 4:
- If the dimension IS shown anywhere in the drawing (even if you cannot read the exact value clearly),
  do NOT flag it — only flag complete absence.
- Do NOT report the dimension value you estimated or read. Only report absence.
- Do NOT flag cover dimensions, unit notation styles, or approximate readings under this check.
- If you are uncertain whether a dimension is shown, assume it is and do NOT flag it.

CHECK 5 — Cross-section bar count & quality
For every circular or rectangular cross-section view visible in the drawing (e.g. SECTION Z-Z for pile,
SECTION A-A, B-B, C-C, D-D for pilecap/pier), do the following:

a) COUNT the filled dot/circle symbols that represent longitudinal reinforcement bars inside that view.
   - Circular pile section: count dots arranged around the ring perimeter.
   - Rectangular pilecap/pier section: count dots along the four sides.
   - If bars appear as closely-spaced PAIRS (bundle bars), count the number of PAIRS — each pair = 1 bundle bar.
   - State which bar mark these dots correspond to (e.g. "x" for pile longitudinal, "g" for pier longitudinal).
   - Set "is_bundle": true if bars are drawn as pairs, or if the word BUNDLE or LEGGED appears near the view.

b) SPACING: Judge whether bars are evenly distributed around the perimeter or show visible gaps/clustering.
   Set "spacing_uniform": false only if there is a clear visual irregularity.

c) ERRONEOUS BOXES: Skip this check — always return an empty array []. Do not flag any boxes.

CHECK 6 — Cross-reference completeness and unlabeled views

a) CUT MARK CROSS-REFERENCE: Look for physical cut-mark symbols — these are DRAWN graphical
   arrows (crossing lines with arrowheads) physically drawn ON a structural plan or elevation
   view to indicate where a cross-section is taken. For each such arrow symbol with a letter
   label (A, B, C, D, etc.), check whether the corresponding section view exists in the drawing.
   Flag if the section view is missing.
   Example: drawn arrow symbols labeled "D" on SECTION B-B but no SECTION D-D view drawn → flag.

   CRITICAL EXCLUSIONS for cut-mark detection:
   - Do NOT flag section names that appear as text within another section's title or label.
     Example: "SECTION Z-Z (PILE) details reference Z1-Z1" — the text "Z1-Z1" here is a
     text reference inside a label, NOT a drawn cut-mark arrow. Do NOT flag Z1-Z1 as missing.
   - Do NOT flag section names referenced in notes or annotations as text.
   - Do NOT flag any section name found in a LAP LENGTH TABLE, SCHEDULE OF REINFORCEMENT,
     TABLE-1, or any other tabular element — tables contain text references only, never
     physical cut-mark arrows.
   - ONLY flag when you see the actual drawn graphical arrow symbol (not text).
   - The "found_on_view" field must name a structural drawing view (e.g. "SECTION B-B FOR
     PILECAP & PIER", "PLAN OF PILECAP") — never a table or schedule.

b) UNLABELED VIEWS: Identify any cross-section, plan, or elevation view that has been drawn but has
   NO title label. Every drawn view must have a label like "SECTION X-X", "PLAN OF...", "DETAIL A", etc.
   Flag each unlabeled drawn view with its approximate location.

Return ONLY valid JSON (no markdown):
{
  "sections": [
    {"name": "SECTION Z-Z (PILE)", "present": true, "scale": "1:30", "bbox": {"x":5,"y":60,"w":18,"h":15}}
  ],
  "notes_check": [
    {"item": "pile_length", "present": true, "value": "12.0 M", "bbox": {"x":40,"y":70,"w":20,"h":3}},
    {"item": "bore_log_ref", "present": false, "value": null, "bbox": null}
  ],
  "label_issues": [
    {"category": "Spelling", "description": "CONTARCTOR should be CONTRACTOR", "suggestion": "Fix spelling", "bbox": {"x":10,"y":55,"w":15,"h":3}}
  ],
  "dimension_issues": [
    {"description": "Pile spacing c/c dimension is not shown anywhere in the plan view", "suggestion": "Add pile c/c spacing dimension to PLAN OF PILECAP", "bbox": {"x":25,"y":20,"w":20,"h":25}}
  ],
  "cross_section_checks": [
    {"section_name": "Z-Z", "component": "pile", "bar_mark": "x", "visual_count": 21, "is_bundle": true, "spacing_uniform": true, "bbox": {"x":5,"y":60,"w":18,"h":15}}
  ],
  "erroneous_boxes": [
    {"description": "Rectangular border enclosing SECTION A-A FOR PILE", "bbox": {"x":3,"y":5,"w":22,"h":20}}
  ],
  "missing_referenced_sections": [
    {"cut_letter": "D", "found_on_view": "SECTION B-B FOR PILECAP & PIER", "missing_section": "SECTION D-D", "bbox": {"x":0,"y":0,"w":60,"h":50}}
  ],
  "unlabeled_views": [
    {"description": "Circular cross-section view adjacent to SECTION C-C has no title label", "bbox": {"x":30,"y":10,"w":12,"h":15}}
  ]
}
"""


SHAPE_DIMS_PROMPT = """From this engineering drawing schedule, extract bar shape dimensions.

Look at the "SHAPE OF BAR" sketch column in the reinforcement schedule table.
Each bar row has a small line-sketch of the bar shape drawn inside that column cell.
Read the numeric labels written DIRECTLY ON the line segments of that sketch — these
are the segment lengths, typically 2–5 numbers per bar (e.g. 825, 4350, 825).

CRITICAL RULES — read carefully:
- Read ONLY numbers that are printed ON the shape sketch lines inside the shape column cell.
- Do NOT read from the LENGTH column, TOTAL LENGTH column, WEIGHT column, or SPACING column.
- Do NOT read overall structural dimensions (pilecap width, pile length, pier height, etc.)
  from any drawing view outside the schedule table.
- Each number should be a bar segment length in mm, typically between 100 mm and 15000 mm.
- Numbers like 1825 in a bar shape sketch that match no logical segment (when the bar's
  other segments are ~825 mm) are likely misreads — omit them rather than guessing.
- If you cannot clearly read a bar's shape sketch, omit that bar entirely (do not guess).
- Return empty {} for any component with no clearly readable shape dimensions.

Return ONLY valid JSON (no markdown):
{
  "shape_dims": {
    "pilecap": {"a": [825, 4350, 825], "b": [825, 4350, 825], "e": [450, 3600]},
    "pile":    {"x": [300, 13125]},
    "pier":    {"g": [500, 8956]}
  }
}
"""


def extract_from_drawing(pdf_bytes: bytes) -> dict:
    """Main entry point. Returns structured drawing data dict."""
    from concurrent.futures import ThreadPoolExecutor

    text_data        = _extract_text(pdf_bytes)
    # Full-page image at 1.0× for the review pass (sections, notes, labels)
    full_images_b64  = _pdf_to_image_b64(pdf_bytes, scale=1.0)
    # Schedule-strip image at 1.5×, cropped to just right of the schedule's
    # leftmost column. Crop is derived from pdfplumber's actual header positions
    # so it works regardless of drawing layout — not a hardcoded fraction.
    _sched_pos = text_data.get('schedule_section_positions', {})
    if _sched_pos:
        _leftmost = min(pos['x'] for pos in _sched_pos.values() if pos)
        _sched_crop = 1.0 - max(0.0, _leftmost - 5.0) / 100.0  # 5 % margin
    else:
        _sched_crop = 0.50  # pdfplumber found nothing → right half as fallback
    sched_images_b64 = _pdf_to_image_b64(pdf_bytes, scale=1.5, crop_right_pct=_sched_crop)

    # Run three API calls in parallel — all finish in ~30s total
    # (1) extraction: schedule/title/notes/TABLE-1 — Haiku/Sonnet, schedule strip, 8192 tok
    # (2) review: CHECK 1-6 — Haiku, full page, 8192 tok
    # (3) shape dims: bar shape sketch dimensions only — Haiku, schedule strip, 2048 tok
    vision_data, review_data, shape_dims_data = None, None, None
    extract_images = sched_images_b64 if sched_images_b64 else full_images_b64
    if extract_images or full_images_b64:
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_extract    = pool.submit(_call_vision, extract_images, EXTRACTION_PROMPT,
                                       EXTRACT_MODEL, 8192)
            f_review     = pool.submit(_call_vision, full_images_b64, REVIEW_PROMPT,
                                       REVIEW_MODEL, 8192)
            f_shape_dims = pool.submit(_call_vision, extract_images, SHAPE_DIMS_PROMPT,
                                       REVIEW_MODEL, 2048)
            vision_data    = f_extract.result()
            review_data    = f_review.result()
            shape_dims_data = f_shape_dims.result()
        log.info('All vision calls complete — extraction=%s review=%s shape_dims=%s',
                 'ok' if vision_data else 'failed',
                 'ok' if review_data else 'failed',
                 'ok' if shape_dims_data else 'failed')

    result = {
        'title_block':                  {},
        'schedule':                     {},
        'schedule_section_bboxes':      {},
        'notes':                        {},
        'table_1':                      [],
        'sections':                     [],
        'notes_check':                  [],
        'label_issues':                 [],
        'dimension_issues':             [],
        'cross_section_checks':         [],
        'erroneous_boxes':              [],
        'missing_referenced_sections':  [],
        'unlabeled_views':              [],
        'raw_text':                     text_data.get('raw_lines', []),
        'schedule_section_positions':   text_data.get('schedule_section_positions', {}),
        'section_view_positions':       text_data.get('section_view_positions', {}),
    }

    if vision_data:
        result['title_block']           = vision_data.get('title_block') or {}
        result['notes']                 = vision_data.get('notes') or {}
        result['table_1']               = vision_data.get('table_1') or []
        result['schedule_section_bboxes'] = vision_data.get('schedule_section_bboxes') or {}
        raw_sched = vision_data.get('schedule') or []
        for row in raw_sched:
            comp = (row.get('component') or 'unknown').lower()
            bm   = (row.get('bar_mark') or '').strip().lower()
            if not bm:
                continue
            comp_dict = result['schedule'].setdefault(comp, {})
            if bm in comp_dict:
                # Same bar mark appears twice (e.g. two 'y' rows for ring zones).
                # Accumulate counts and total lengths; keep other fields from first row.
                existing = comp_dict[bm]
                def _add_field(key):
                    a = _norm_float(existing.get(key))
                    b = _norm_float(row.get(key))
                    if a is not None and b is not None:
                        existing[key] = a + b
                _add_field('count')
                _add_field('total_length_m')
                _add_field('total_wt_kg')
            else:
                comp_dict[bm] = row

    # Merge shape dimensions from the dedicated lightweight call into each bar's schedule row.
    if shape_dims_data:
        for comp, bars in (shape_dims_data.get('shape_dims') or {}).items():
            comp = comp.lower()
            for bm, dims in (bars or {}).items():
                bm = bm.strip().lower()
                bar_row = result['schedule'].get(comp, {}).get(bm)
                if bar_row and isinstance(bar_row, dict) and isinstance(dims, list) and dims:
                    bar_row['shape_dimensions'] = dims

    if review_data:
        result['sections']                    = review_data.get('sections')                    or []
        result['notes_check']                 = review_data.get('notes_check')                 or []
        result['label_issues']                = review_data.get('label_issues')                or []
        result['dimension_issues']            = review_data.get('dimension_issues')            or []
        result['cross_section_checks']        = review_data.get('cross_section_checks')        or []
        result['erroneous_boxes']             = review_data.get('erroneous_boxes')             or []
        result['missing_referenced_sections'] = review_data.get('missing_referenced_sections') or []
        result['unlabeled_views']             = review_data.get('unlabeled_views')             or []

    # Fill title block / notes gaps from pdfplumber text
    for key, val in text_data.get('title_block', {}).items():
        if not result['title_block'].get(key):
            result['title_block'][key] = val
    for key, val in text_data.get('notes', {}).items():
        if not result['notes'].get(key):
            result['notes'][key] = val

    return result


# ── pdfplumber text pass ──────────────────────────────────────────────────────

def _extract_text(pdf_bytes: bytes) -> dict:
    title_block = {}
    notes = {}
    raw_lines = []

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                words = page.extract_words() or []
                rows = {}
                for w in words:
                    y = round(w['top'] / 4) * 4
                    rows.setdefault(y, []).append(w)

                for y in sorted(rows):
                    line = ' '.join(w['text'] for w in sorted(rows[y], key=lambda x: x['x0']))
                    raw_lines.append(line)

                    # Title block fields
                    if 'DRG. NO.' in line or 'IND /' in line:
                        m = re.search(r'IND\s*/\s*\w+\s*/\s*\w+\s*-\s*\w+', line)
                        if m:
                            title_block['drawing_number'] = re.sub(r'\s+', '', m.group()).replace('/', '/')
                    if re.search(r'\bR\d+\b', line) and 'DATE' not in line and len(line) < 10:
                        title_block['revision'] = line.strip()
                    if '30.0M' in line or '25.0M' in line:
                        m = re.search(r'(\d+\.\d+M\s*[-–]\s*\d+\.\d+M)', line)
                        if m:
                            title_block['spans'] = m.group(1)
                        m2 = re.search(r'(\d+\.\d+M)\s+WIDE', line)
                        if m2:
                            title_block['width'] = m2.group(1)
                    if re.search(r'FOR PIER\s+P\d+\s+TO\s+P\d+', line, re.IGNORECASE):
                        m = re.search(r'P\d+\s+TO\s+P\d+', line, re.IGNORECASE)
                        if m:
                            title_block['pier_range'] = m.group()
                    if 'DRAWN BY' in line:
                        pass  # next line has the name; hard to do in single-line pass
                    # Names: look for X.Y.NAME pattern anywhere in line
                    for name_m in re.finditer(r'[A-Z]\.[A-Z]\.\w+', line):
                        name = name_m.group()
                        # Determine role by x-position of the name word relative to line layout
                        # Heuristic: if 'DESIGN BY' appears earlier in the same line → approved_by
                        # Otherwise assign to first unfilled slot
                        if 'DESIGN BY' in line:
                            if not title_block.get('approved_by'):
                                title_block['approved_by'] = name
                        elif not title_block.get('drawn_by'):
                            title_block['drawn_by'] = name
                        elif not title_block.get('design_by'):
                            title_block['design_by'] = name
                    if re.match(r'\d{2}-\d{2}-\d{4}', line):
                        title_block['date'] = line.strip()
                    if 'AS SHOWN' in line:
                        title_block['scale'] = 'AS SHOWN'
                    if 'DETAILS OF PILE' in line:
                        title_block['title'] = line.strip()

                    # Notes
                    m = re.search(r'LAP\s+LENGTH\s+FOR\s+BAR\s+FOR\s+(M\d+)', line, re.IGNORECASE)
                    if m:
                        notes['lap_length_concrete_grade'] = m.group(1).upper()

                    # Detect a generic concrete grade note (e.g. "Concrete Mix M35" → applies to all)
                    m = re.search(r'(?:concrete\s+(?:mix|grade)|all\s+concrete)\s+(M\d+)', line, re.IGNORECASE)
                    if not m:
                        m = re.search(r'\bM(\d+)\b', line)
                        if m and len(line.split()) <= 4:  # short line — likely a grade-only note
                            m = re.search(r'\b(M\d+)\b', line)
                    if m:
                        grade = m.group(1).upper() if m.lastindex == 1 else ('M' + m.group(1)).upper()
                        for comp in ('pile', 'pilecap', 'pier'):
                            notes.setdefault(f'concrete_{comp}', grade)

    except Exception as e:
        raw_lines.append(f'[pdfplumber error: {e}]')

    schedule_section_positions, section_view_positions = {}, {}
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if pdf.pages:
                schedule_section_positions, section_view_positions = \
                    _extract_positions(pdf.pages[0])
    except Exception as e:
        log.warning('_extract_positions failed: %s', e)


    return {
        'title_block':              title_block,
        'notes':                    notes,
        'raw_lines':                raw_lines,
        'schedule_section_positions': schedule_section_positions,
        'section_view_positions':   section_view_positions,
    }


# ── pdfplumber position extraction ───────────────────────────────────────────


def _extract_positions(page) -> tuple:
    """
    Extract schedule section header positions and section view label positions
    from pdfplumber word bounding boxes (PDF point coordinates → percentages).
    Returns (schedule_section_positions, section_view_positions).
    """
    pw, ph = page.width, page.height
    words = page.extract_words() or []
    schedule_x_min = pw * 0.55   # schedule is always in the right ~38% of the drawing

    def to_pct(x0, top, x1, bottom):
        return {
            'x': round(x0 / pw * 100, 2),
            'y': round(top / ph * 100, 2),
            'w': round((x1 - x0) / pw * 100, 2),
            'h': round((bottom - top) / ph * 100, 2),
        }

    # ── 1. Find schedule component section headers ───────────────────────────
    COMP_KEYWORDS = {'pilecap': 'PILECAP', 'pile': 'PILE', 'pier': 'PIER'}
    comp_header_y = {}   # comp → top y in PDF points
    schedule_words_x = []
    for w in words:
        if w['x0'] < schedule_x_min:
            continue
        schedule_words_x.append(w['x0'])
        text = w['text'].strip().upper()
        for comp, keyword in COMP_KEYWORDS.items():
            if text == keyword and comp not in comp_header_y:
                comp_header_y[comp] = w['top']
                break

    sorted_comps = sorted(comp_header_y.items(), key=lambda kv: kv[1])
    sect_x0 = min(schedule_words_x) if schedule_words_x else schedule_x_min

    schedule_section_positions = {}
    for idx, (comp, header_y) in enumerate(sorted_comps):
        y_end = sorted_comps[idx + 1][1] if idx + 1 < len(sorted_comps) else ph * 0.82
        schedule_section_positions[comp] = to_pct(sect_x0, header_y, pw, y_end)

    # ── 2. Find section view labels on the left side ─────────────────────────
    line_words: dict = {}
    for w in words:
        if w['x0'] >= schedule_x_min:
            continue
        line_y = round(w['top'] / 3) * 3
        line_words.setdefault(line_y, []).append(w)

    section_view_positions = {}
    TRIGGER_WORDS = {'SECTION', 'TABLE-1', 'LAP', 'NOTES', 'DETAIL'}
    for line_y, lw in sorted(line_words.items()):
        lw_sorted = sorted(lw, key=lambda x: x['x0'])
        line_text = ' '.join(w['text'] for w in lw_sorted).upper().strip()
        if any(t in line_text for t in TRIGGER_WORDS):
            x0  = min(w['x0']     for w in lw_sorted)
            x1  = max(w['x1']     for w in lw_sorted)
            top = min(w['top']    for w in lw_sorted)
            view_h_pts = ph * 0.18
            name = line_text[:60]
            section_view_positions[name] = to_pct(x0, top, x1, top + view_h_pts)

    log.info(
        '_extract_positions: schedule_sections=%s section_views=%d',
        list(schedule_section_positions.keys()),
        len(section_view_positions),
    )
    return schedule_section_positions, section_view_positions


# ── Claude vision pass ────────────────────────────────────────────────────────

def _pdf_to_image_b64(pdf_bytes: bytes, scale: float = 1.0,
                       crop_right_pct: float = None) -> list:
    """Render each PDF page to a PNG and return list of base64 strings.

    crop_right_pct: if set (e.g. 0.40), crop the image to the rightmost fraction
    of the page width before encoding. Used to isolate the schedule strip at higher
    resolution without sending the full large image.
    """
    try:
        import fitz  # PyMuPDF
        log.info('PyMuPDF rendering PDF (%d bytes) scale=%.1f crop_right=%.0f%%',
                 len(pdf_bytes), scale, (crop_right_pct or 0) * 100)
        doc = fitz.open(stream=pdf_bytes, filetype='pdf')
        images = []
        for i, page in enumerate(doc):
            mat = fitz.Matrix(scale, scale)
            pix = page.get_pixmap(matrix=mat)

            if crop_right_pct and 0 < crop_right_pct < 1.0:
                # Render only the right strip of the page in page-space coordinates,
                # then re-render at the target scale so PyMuPDF does the crop natively.
                page_w = page.rect.width
                clip_page = fitz.Rect(
                    page_w * (1.0 - crop_right_pct), 0,
                    page_w, page.rect.height
                )
                pix = page.get_pixmap(matrix=mat, clip=clip_page)
                log.info('Page %d cropped to right %.0f%%: %dx%d px',
                         i+1, crop_right_pct * 100, pix.width, pix.height)
            else:
                log.info('Page %d rendered: %dx%d px', i+1, pix.width, pix.height)

            b64 = base64.standard_b64encode(pix.tobytes('png')).decode()
            log.info('Page %d b64 len=%d', i+1, len(b64))
            images.append(b64)
        doc.close()
        return images
    except ImportError as e:
        log.error('PyMuPDF (fitz) not installed: %s', e)
        print(f'[ED Checker] PyMuPDF import failed: {e}', flush=True)
        return []
    except Exception as e:
        log.error('PDF rendering error: %s', e, exc_info=True)
        print(f'[ED Checker] PDF rendering error: {e}', flush=True)
        return []


def _parse_json_with_repair(raw: str) -> dict | None:
    """Try direct JSON parse; if truncated, close open structures and retry."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Walk the string tracking bracket/brace depth and string state,
    # recording the position after each top-level array item completes.
    # Then close all open structures from the last safe point.
    depth = 0
    in_str = False
    escape = False
    last_safe = 0

    for i, ch in enumerate(raw):
        if escape:
            escape = False
            continue
        if ch == '\\' and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in '{[':
            depth += 1
        elif ch in '}]':
            depth -= 1
            if depth == 1:          # just closed one top-level item
                last_safe = i + 1

    if last_safe == 0:
        return None

    # Truncate at last complete item and close outer array + object
    fragment = raw[:last_safe].rstrip().rstrip(',')

    # Count still-open brackets to close them
    open_sq = fragment.count('[') - fragment.count(']')
    open_cu = fragment.count('{') - fragment.count('}')
    fragment += ']' * open_sq + '}' * open_cu

    try:
        result = json.loads(fragment)
        log.warning('JSON repaired by truncating at char %d (original length %d)',
                    last_safe, len(raw))
        return result
    except json.JSONDecodeError:
        return None


def _call_vision(images_b64: list, prompt: str, model: str = 'claude-sonnet-4-6',
                  max_tokens: int = 8192) -> dict | None:
    """Send pre-rendered page images + prompt to Claude. Returns parsed JSON or None."""
    if not ANTHROPIC_API_KEY or not images_b64:
        return None
    raw = ''
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        log.info('Vision call: model=%s sending %d image(s), prompt length=%d chars',
                 model, len(images_b64[:1]), len(prompt))

        content = [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': b64}}
            for b64 in images_b64[:1]
        ]
        content.append({'type': 'text', 'text': prompt})

        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{'role': 'user', 'content': content}],
        )
        raw = response.content[0].text.strip()
        log.info('Vision response: length=%d chars, stop_reason=%s', len(raw), response.stop_reason)

        if response.stop_reason == 'max_tokens':
            log.warning('Response hit max_tokens — attempting JSON repair')

        if raw.startswith('```'):
            raw = re.sub(r'^```(?:json)?\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw)

        parsed = _parse_json_with_repair(raw)
        if parsed is None:
            raise json.JSONDecodeError('Could not parse or repair JSON', raw, 0)

        log.info('Vision call OK — top-level keys: %s', list(parsed.keys()))
        return parsed

    except json.JSONDecodeError as e:
        log.error('Non-JSON response: %s … (first 200: %s)', e, raw[:200])
        print(f'[ED Checker] Vision JSON parse error: {e}', flush=True)
        return None
    except Exception as e:
        log.error('Claude API error: %s', e, exc_info=True)
        print(f'[ED Checker] Claude API error: {e}', flush=True)
        return None
