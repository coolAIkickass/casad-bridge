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

EXTRACTION_PROMPT = """You are analyzing a CASAD AutoCAD engineering drawing for a bridge structure (Pile-Pilecap-Pier foundation).

IMPORTANT: For every item you extract, also estimate its bounding box as a percentage of the full image dimensions:
  "bbox": {"x": <left edge %>, "y": <top edge %>, "w": <width %>, "h": <height %>}
This is used to highlight the exact location in the drawing when an error is flagged.

Extract ALL of the following as precisely as possible:

1. SCHEDULE OF REINFORCEMENT — multiple sections may exist (Pilecap, Pile per pile, Pier).
   For EACH row in any schedule table, extract:
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
     "total_wt_kg": 2174.77,
     "bbox": {"x": 65.0, "y": 32.5, "w": 30.0, "h": 2.5}
   }
   Note: spacing_mm is null for longitudinal bars (marked "-"). For stirrups/rings extract the c/c spacing.
   For count like "4×13 = 52", set count=52 and count_text="4×13 = 52".
   The bbox should cover just that single row in the schedule table.

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
  "title_block": {...},
  "notes": {...},
  "table_1": [...]
}
Use null for any value not found or not legible.
"""


def extract_from_drawing(pdf_bytes: bytes) -> dict:
    """Main entry point. Returns structured drawing data dict."""
    text_data = _extract_text(pdf_bytes)
    vision_data = _extract_via_vision(pdf_bytes)

    # Merge: vision data is primary, text_data fills gaps
    result = {
        'title_block': {},
        'schedule': {},
        'notes': {},
        'table_1': [],
        'raw_text': text_data.get('raw_lines', []),
    }

    if vision_data:
        result['title_block'] = vision_data.get('title_block') or {}
        result['notes']       = vision_data.get('notes') or {}
        result['table_1']     = vision_data.get('table_1') or []
        # Index schedule by component → bar_mark
        raw_sched = vision_data.get('schedule') or []
        for row in raw_sched:
            comp = (row.get('component') or 'unknown').lower()
            bm   = (row.get('bar_mark') or '').strip().lower()
            if not bm:
                continue
            result['schedule'].setdefault(comp, {})[bm] = row

    # Fill title block gaps from pdfplumber text
    tb = result['title_block']
    for key, val in text_data.get('title_block', {}).items():
        if not tb.get(key):
            tb[key] = val

    # Fill notes gaps
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

                    for comp, pattern in [('pile', r'\(M40\)'), ('pilecap', r'\(M35\)'), ('pier', r'\(M35\)')]:
                        if re.search(pattern, line):
                            notes.setdefault(f'concrete_{comp}', pattern[1:-1])

    except Exception as e:
        raw_lines.append(f'[pdfplumber error: {e}]')

    return {'title_block': title_block, 'notes': notes, 'raw_lines': raw_lines}


# ── Claude vision pass ────────────────────────────────────────────────────────

def _pdf_to_image_b64(pdf_bytes: bytes) -> list:
    """Render each PDF page to a PNG and return list of base64 strings."""
    try:
        import fitz  # PyMuPDF
        log.info('PyMuPDF imported OK, rendering PDF (%d bytes)', len(pdf_bytes))
        doc = fitz.open(stream=pdf_bytes, filetype='pdf')
        log.info('PDF opened: %d pages', doc.page_count)
        images = []
        for i, page in enumerate(doc):
            mat = fitz.Matrix(1.0, 1.0)
            pix = page.get_pixmap(matrix=mat)
            b64 = base64.standard_b64encode(pix.tobytes('png')).decode()
            log.info('Page %d rendered: %dx%d px, b64 len=%d', i+1, pix.width, pix.height, len(b64))
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


def _extract_via_vision(pdf_bytes: bytes) -> dict | None:
    if not ANTHROPIC_API_KEY:
        return None
    images_b64 = _pdf_to_image_b64(pdf_bytes)
    if not images_b64:
        log.error('No images rendered from PDF — vision extraction aborted')
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        log.info('Sending %d page image(s) to Claude vision API', len(images_b64[:3]))

        content = []
        for b64 in images_b64[:3]:
            content.append({
                'type': 'image',
                'source': {'type': 'base64', 'media_type': 'image/png', 'data': b64},
            })
        content.append({'type': 'text', 'text': EXTRACTION_PROMPT})

        response = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=8192,   # increased from 4096 — full PPP schedule needs ~6-7k tokens
            messages=[{'role': 'user', 'content': content}],
        )
        raw = response.content[0].text.strip()
        log.info('Claude vision response received, length=%d chars, stop_reason=%s',
                 len(raw), response.stop_reason)

        if response.stop_reason == 'max_tokens':
            log.warning('Response hit max_tokens limit — JSON may be truncated, attempting repair')

        # Strip markdown fences if present
        if raw.startswith('```'):
            raw = re.sub(r'^```(?:json)?\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw)

        parsed = _parse_json_with_repair(raw)
        if parsed is None:
            raise json.JSONDecodeError('Could not parse or repair JSON', raw, 0)

        schedule_rows = len(parsed.get('schedule') or [])
        log.info('Vision extraction OK — schedule rows=%d', schedule_rows)
        return parsed

    except json.JSONDecodeError as e:
        log.error('Claude returned non-JSON: %s … (first 200 chars: %s)', e, raw[:200])
        print(f'[ED Checker] Vision JSON parse error: {e}', flush=True)
        return None
    except Exception as e:
        log.error('Claude vision API error: %s', e, exc_info=True)
        print(f'[ED Checker] Claude API error: {e}', flush=True)
        return None
