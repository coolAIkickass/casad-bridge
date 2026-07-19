"""
Extract structured data from an AutoCAD engineering drawing PDF.
Step 1: pdfplumber text extraction (title block, note labels, grades).
Step 2: PyMuPDF → image → Claude vision API (schedule tables, notes).
"""
import io
import os
import re
import json
import base64
import logging
import pdfplumber
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeoutError

from .profiles import PPP_PROFILE, TRIGGER_WORDS
from .schema import new_drawing_data

log = logging.getLogger(__name__)
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# ── Text-extraction constants ─────────────────────────────────────────────────
# Drawing-type knowledge lives in profiles.py and is shared with dxf_extractor —
# these aliases keep the function bodies below unchanged.
_REQUIRED_PPP_SECTIONS = PPP_PROFILE.required_sections
_NOTE_KEYWORDS = PPP_PROFILE.note_keywords


def _norm_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
# Use ED_MODEL env var to override: 'haiku' (cheap, for testing) or 'sonnet' (production)
_model_alias = os.environ.get('ED_MODEL', 'haiku').lower()
EXTRACT_MODEL = 'claude-haiku-4-5-20251001' if _model_alias == 'haiku' else 'claude-sonnet-4-6'
REVIEW_MODEL  = 'claude-haiku-4-5-20251001'  # always Haiku — review call doesn't need Sonnet
# Engineering Reasoning Reviewer uses the same tier as extraction, not Haiku like
# REVIEW_MODEL above — open-ended holistic reasoning (does this layout make sense,
# does this look like a construction joint) benefits from a stronger model far more
# than the mechanical pattern-matching CHECK 1-7 checks do. Real, explicit cost
# increase per review — one more paid API call, at the extraction-tier model.
ENGINEERING_REVIEW_MODEL = EXTRACT_MODEL

SCHEDULE_PROMPT_TEMPLATE = """You are analyzing reinforcement schedule tables from a CASAD bridge engineering drawing.

IMAGE LAYOUT:
{IMAGE_MAP}

PART A — SCHEDULE ROWS
For EACH row in the schedule tables, extract:
{{
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
}}

COLUMN MAPPING RULES — read carefully:
- bar_dia_mm: the φ / DIA column — always a small integer (8, 10, 12, 16, 20, 25, 32). Do NOT confuse with length or spacing.
- spacing_mm: the c/c spacing column — null for longitudinal bars (shown as "-"). For rings/stirrups this is the pitch in mm.
- length_m: the individual bar LENGTH in metres — a decimal like 9.160, 4.049, 13.425. Do NOT use spacing or weight here.
- count: the total number of bars. May contain multiplication expressions.
  ALWAYS use the value AFTER the "=" sign as the count — it is the total.
  Examples: "4×13 = 52" → count=52, count_text="4×13 = 52"
            "21 * 4 = 84" → count=84, count_text="21 * 4 = 84"
            "6 × 21 = 126" → count=126, count_text="6 × 21 = 126"
  Both "×" and "*" are multiplication symbols. The number BEFORE "=" is never the count.
- If the same bar mark appears in two rows (e.g. two 'y' rows for different ring zones), return BOTH rows separately.

BAR MARK COMPLETENESS — extract ALL rows including suffixed marks:
k1, j1, i1, f1, y1, x1, z — scan to the bottom of each component section. Do not stop early.

PART B — SHAPE DIMENSIONS
Also look at the "SHAPE OF BAR" sketch column in each row. Each row has a small line-sketch of
the bar shape with numeric labels written ON the line segments.
Read ONLY numbers printed ON the shape sketch lines inside the shape column cell.

CRITICAL RULES for shape dimensions:
- Do NOT read from the LENGTH, TOTAL LENGTH, WEIGHT, or SPACING columns.
- Each shape segment length is typically between 100 mm and 15000 mm.
- BAR MARK DIGIT BLEED: Bar marks with numeric suffixes (f1, y1, i1, j1, k1, x1, d1) appear
  immediately LEFT of the shape sketch column. The trailing digit is NOT part of a shape dimension.
  Example: bar mark "f1", sketch shows "300" → read 300, NOT 1300.
  A 3-digit segment must remain 3 digits — do not prepend any digit from the bar mark label.
- If you cannot clearly read a bar's shape sketch, omit that bar entirely (do not guess).
- Return empty {{}} for any component with no clearly readable shape dimensions.

Return ONLY valid JSON (no markdown, no extra text):
{{
  "schedule": [
    {{"bar_mark": "a", "component": "pilecap", "bar_dia_mm": 25, "spacing_mm": null,
     "count_text": "42", "count": 42, "length_m": 13.425, "total_length_m": 563.85,
     "unit_wt_kg_m": 3.857, "total_wt_kg": 2174.77, "reinforcement_text": "25Φ – 42 NOS."}}
  ],
  "shape_dims": {{
    "pilecap": {{"a": [825, 4350, 825], "b": [825, 4350, 825], "e": [450, 3600]}},
    "pile":    {{"x": [300, 13125]}},
    "pier":    {{"g": [500, 8956]}}
  }}
}}
Use null for any schedule value not found or not legible.
"""

TITLE_PROMPT = """You are analyzing a CASAD AutoCAD engineering drawing for a bridge structure (Pile-Pilecap-Pier foundation).
Extract the title block and notes from this image (right-side strip of the drawing).

1. TITLE BLOCK — extract:
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

2. NOTES — extract these specific values if present:
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

Return ONLY valid JSON (no markdown, no extra text):
{
  "title_block": {...},
  "notes": {...}
}
Use null for any value not found or not legible.
"""

REVIEW_PROMPT_TEMPLATE = """You are doing a quality review of a CASAD bridge engineering drawing (Pile-Pilecap-Pier foundation type).

Scan the ENTIRE drawing image carefully and return findings for each check below.
For every finding include a bbox: {{"x": <left%>, "y": <top%>, "w": <width%>, "h": <height%>}} as % of image dimensions.

The following section/view labels were extracted from the PDF text layer (authoritative — do not second-guess presence of these views):
{SECTION_LABELS}

CHECK 3 — Label & annotation quality
The label text above was read directly from the PDF — use these strings (not the image) to check spelling.
Flag GENUINE ERRORS ONLY:
- Spelling errors in the labels listed above: only flag if the misspelling is unambiguous. Do NOT flag
  CONTRACTOR, REINFORCEMENT, FOUNDATION, or other common engineering words unless you can see the
  misspelling character-by-character with certainty.
- Bar mark labels in section views that don't match the schedule
- Scale labels that are clearly inconsistent with each other
- Any label that clearly points to the wrong component
- Concrete grade mismatch: if a section view or plan view has a concrete grade annotation
  (e.g. "M35", "M50") directly labelled on the structural drawing, cross-check it against
  the grade stated in the NOTES section of the same drawing. If they differ, flag it.

STRICT EXCLUSIONS — do NOT flag any of the following:
- "SECTION A-A FOR PILE" vs "SECTION A-A FOR PILECAP & PIER" — intentional distinct names.
- "BUNDLE BARS" — correct engineering term; do not flag as grammar issue.
- Dimension values, units, or notation styles that follow standard structural drawing conventions.
- Text density, font size, or legibility observations about schedule tables.
- Long NOTES section sentences (e.g. "SCHEDULE OF REINFORCEMENT IS ONLY FOR GUIDANCE...") that
  appear to end mid-word at a line boundary — AutoCAD MTEXT word-wraps long text and the continuation
  appears in the next line or entity. Do NOT flag these as truncated text.
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
SECTION A-A, B-B, C-C for pilecap/pier), do the following:

a) COUNT the filled dot/circle symbols that represent longitudinal reinforcement bars inside that view.
   - Circular pile section: count dots arranged around the ring perimeter.
   - Rectangular pilecap/pier section: count dots along the four sides.
   - If bars appear as closely-spaced PAIRS (bundle bars), count the number of PAIRS — each pair = 1 bundle bar.
   - State which bar mark these dots correspond to (e.g. "x" for pile longitudinal, "g" for pier longitudinal).
   - Set "is_bundle": true if bars are drawn as pairs, or if the word BUNDLE or LEGGED appears near the view.

b) SPACING — Detailed spacing analysis for each cross-section:

   For CIRCULAR sections (pile, e.g. SECTION Z-Z):
   - Imagine a clock face overlaid on the ring. Note the approximate clock positions of all visible bars/pairs.
   - Compute the expected angular gap = 360° ÷ bar_count (use pair_count if bundle bars).
   - For EACH irregularity found, add one entry to spacing_issues:
     • "clustering": two consecutive bars/pairs that appear visibly closer together than the average gap
     • "gap": an arc more than ~1.5× the expected gap with no bar
     • "missing_bar": geometry strongly implies a bar should exist but none is drawn
   - For bundle-bar PAIRS: treat each pair as one unit.

   For RECTANGULAR sections (pilecap/pier):
   - Count bars on each side (top, bottom, left, right).
   - Flag if bars on one side are clustered to one corner, leaving a visible gap on the opposite end.

   Return an array — empty [] if spacing looks fully uniform:
   "spacing_issues": [
     {{"type": "clustering" | "gap" | "missing_bar", "location": "approx 7–8 o'clock", "description": "..."}}
   ]
   Also set "spacing_uniform": false if spacing_issues is non-empty, true if empty.

c) ERRONEOUS BOXES — scan for rectangular outlines accidentally left by the drafter:
- Unlabeled, empty rectangles with no structural content (bars, hatching, dimensions) inside
- A callout box ("DETAILS A" etc.) whose boundary clearly overlaps or cuts into an adjacent view
- Any isolated rectangle that does not match the boundary of any labeled view, plan, or table
Do NOT flag: standard view borders around labeled section/plan views, table grid lines,
title block border, drawing sheet border, or any rectangle that encloses recognisable
structural drawing content (reinforcement bars, dimensions, hatching).
Return each stray box as: {{"description": "...", "bbox": {{"x":...,"y":...,"w":...,"h":...}}}}
Empty [] if none found.

CHECK 6 — Unlabeled views and missing sections
The following section/view labels were confirmed present (extracted from drawing text — treat as authoritative):
{SECTION_LABELS}

The following cut-mark letters were found in the drawing but have NO corresponding confirmed section view — they are either drawn but unlabeled, or the section is completely absent:
{UNRESOLVED_CUTS}

TASK — do ALL of the following:

Step A — For EACH unresolved cut letter listed above:
- Visually scan the entire drawing for a cross-section circle, rectangular section, or elevation view that corresponds to that cut letter.
- If you find a drawn view WITHOUT a title label → add to unlabeled_views (describe which cut letter it corresponds to).
- If NO drawn view exists anywhere for it → do NOT add to unlabeled_views (missing sections are handled separately).

Step B — Scan the ENTIRE drawing for any drawn structural view (circular pile cross-section, rectangular pilecap/pier section, elevation, plan, detail) that:
  • Is NOT in the confirmed label list above, AND
  • Has NO title label (e.g. no "SECTION X-X", "PLAN OF …", "DETAIL A" text directly below or above it).
  Flag each as a separate unlabeled_views entry.

Step C — Check ALL confirmed labels above for consistency:
  • A view labeled "SECTION C-C FOR PILE" must show a pile cross-section, not a pilecap or pier.
  • If the label clearly describes the WRONG component for what is drawn, flag it in label_issues.

CHECK 7 — Engineering knowledge-base rules
The following rules come from CASAD's own design methodology and the IRC/IS codes it's built
on. Each one requires visual judgment to evaluate (unlike the deterministic checks already run
separately from the DXF/schedule data) — a rule listed here could not be reduced to a simple
formula/threshold, or has not yet been validated against enough real drawings to be enforced as
a hard check. Only flag a finding if you are CONFIDENT the drawing shows the described issue —
these are exploratory/lower-confidence checks by design; omit rather than guess.
knowledge_rule_findings is a list of CONFIRMED VIOLATIONS ONLY — never add an entry to report
that you checked a rule and it passed, to share your reasoning about a rule you couldn't fully
verify, or to note "no issue found" / "appears compliant" / "no violation detected". Treat each
rule below exactly like every other check in this prompt: either you found a specific, concrete
defect (report it), or you didn't (say nothing about that rule at all). If no rule below is
violated, knowledge_rule_findings must be an empty array — do not populate it with your analysis
notes on rules you found no problem with.
{KNOWLEDGE_RULES}

Return ONLY valid JSON (no markdown):
{{
  "label_issues": [
    {{"category": "Spelling", "description": "CONTARCTOR should be CONTRACTOR", "suggestion": "Fix spelling", "bbox": {{"x":10,"y":55,"w":15,"h":3}}}}
  ],
  "dimension_issues": [
    {{"description": "Pile spacing c/c dimension is not shown anywhere in the plan view", "suggestion": "Add pile c/c spacing dimension to PLAN OF PILECAP", "bbox": {{"x":25,"y":20,"w":20,"h":25}}}}
  ],
  "cross_section_checks": [
    {{"section_name": "Z-Z", "component": "pile", "bar_mark": "x", "visual_count": 21, "is_bundle": true, "spacing_uniform": true, "spacing_issues": [], "bbox": {{"x":5,"y":60,"w":18,"h":15}}}}
  ],
  "erroneous_boxes": [
    {{"description": "Unlabeled empty rectangle overlapping SECTION A-A", "bbox": {{"x":5,"y":10,"w":12,"h":8}}}}
  ],
  "unlabeled_views": [
    {{"description": "Cross-section view for cut letter D is drawn but has no SECTION D-D title label", "bbox": {{"x":30,"y":10,"w":12,"h":15}}}}
  ],
  "knowledge_rule_findings": [
    {{"rule_id": "IRC112-17.3.1-PILECAP-DUCTILE-EXEMPTION", "description": "Pilecap stirrup spacing tightens near the north pile cluster with no punching-shear justification visible", "bbox": {{"x":15,"y":30,"w":20,"h":15}}}}
  ]
}}
"""



def extract_from_drawing(pdf_bytes: bytes) -> dict:
    """Main entry point. Returns structured drawing data dict."""
    from concurrent.futures import ThreadPoolExecutor

    text_data        = _extract_text_with_timeout(pdf_bytes)

    section_labels   = text_data.get('all_label_text', [])
    cut_letters      = text_data.get('cut_letters', set())
    section_view_pos = text_data.get('section_view_positions', {})
    text_missing     = _text_missing_sections(cut_letters, section_view_pos)

    # Full-page image at 2.0× for the review pass.
    # 1.3× made section circles ~150 px in diameter — too small to count bundle bar pairs
    # accurately (Claude undercounts by 15-25%). 2.0× gives ~230 px circles and reliable
    # counting. PNG stays ~1.2 MB (well within API and Render memory limits).
    full_images_b64  = _pdf_to_image_b64(pdf_bytes, scale=2.0)
    # Schedule-strip image at 1.5×, cropped to just right of the schedule's leftmost column.
    _sched_pos = text_data.get('schedule_section_positions', {})
    if _sched_pos:
        _leftmost = min(pos['x'] for pos in _sched_pos.values() if pos)
        _sched_crop = 1.0 - max(0.0, _leftmost - 5.0) / 100.0
    else:
        _sched_crop = 0.50
    sched_images_b64 = _pdf_to_image_b64(pdf_bytes, scale=1.5, crop_right_pct=_sched_crop)

    # Per-component band images at 2.5× for schedule row + shape dimension extraction.
    # Each component section is cropped individually so numeric labels are clearly readable.
    # Falls back to the schedule strip if pdfplumber found no section positions.
    shape_band_images = []
    _band_comps = []   # tracks which component each band image corresponds to
    if _sched_pos:
        try:
            for comp in ('pilecap', 'pile', 'pier'):
                pos = _sched_pos.get(comp)
                if not pos:
                    continue
                if pos.get('h', 0) <= 0:
                    log.warning('Skipping %s band — non-positive height %.2f', comp, pos.get('h', 0))
                    continue
                clip = (
                    max(0.0, (pos['x'] - 2.0) / 100),   # 2% left margin (captures bar mark col)
                    max(0.0, (pos['y'] - 1.0) / 100),   # 1% top margin
                    min(1.0, (pos['x'] + pos['w'] + 1.0) / 100),
                    min(1.0, (pos['y'] + pos['h'] + 8.0) / 100),  # 8% bottom — last section can extend past 82% hardcoded limit
                )
                imgs = _pdf_to_image_b64(pdf_bytes, scale=2.5, clip_rect_pct=clip)
                shape_band_images.extend(imgs)
                _band_comps.append(comp)
            # Require at least 2 valid component bands; a single band usually means
            # pdfplumber found a false positive but missed most schedule sections.
            if len(shape_band_images) >= 2:
                log.info('Schedule bands: %d component band image(s) at 2.5× (%s)',
                         len(shape_band_images), _band_comps)
            else:
                log.warning('Only %d valid band(s) found (%s) — falling back to schedule strip',
                            len(shape_band_images), _band_comps)
                shape_band_images = []
                _band_comps = []
        except Exception as e:
            log.warning('Component band rendering failed (%s) — falling back to schedule strip', e)
            shape_band_images = []
            _band_comps = []
    if not shape_band_images:
        shape_band_images = sched_images_b64 or full_images_b64

    # Build dynamic image map for SCHEDULE_PROMPT so Claude knows which component each image covers.
    if _band_comps:
        _image_map = '\n'.join(
            f'- Image {i+1} = {c.upper()} schedule rows (high-res crop of that component block)'
            for i, c in enumerate(_band_comps)
        )
    else:
        _image_map = ('- 1 image: the full schedule strip — '
                      'identify components by their header labels (PILECAP / PILE / PIER).')
    schedule_prompt = SCHEDULE_PROMPT_TEMPLATE.format(IMAGE_MAP=_image_map)

    # Run three API calls in parallel:
    # (1) schedule: rows + shape dims — Haiku/Sonnet, 2.5× component bands, 8192 tok
    # (2) title: title block/notes — Haiku, 1.5× schedule strip, 4096 tok
    # (3) review: CHECK 3-6 — Haiku, 2.5× full page, 8192 tok
    schedule_data, title_data, review_data = None, None, None
    title_images = sched_images_b64 or full_images_b64
    if shape_band_images or full_images_b64:
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_schedule = pool.submit(_call_vision, shape_band_images, schedule_prompt,
                                     EXTRACT_MODEL, 8192,
                                     max_images=len(shape_band_images))
            f_title    = pool.submit(_call_vision, title_images, TITLE_PROMPT,
                                     REVIEW_MODEL, 4096)
            f_review   = pool.submit(run_review_vision, pdf_bytes,
                                     section_labels, text_missing, full_images_b64)
            schedule_data = f_schedule.result()
            title_data    = f_title.result()
            review_data   = f_review.result()
        log.info('All vision calls complete — schedule=%s title=%s review=%s',
                 'ok' if schedule_data else 'failed',
                 'ok' if title_data else 'failed',
                 'ok' if review_data else 'failed')

    # Schema-conformant result; the PDF/vision path keeps the default capabilities
    # (everything claimable — see schema.DEFAULT_CAPABILITIES).
    result = new_drawing_data(
        missing_referenced_sections=text_missing,   # from pdfplumber text (authoritative)
        sections_from_text=text_data.get('sections_from_text', []),
        notes_completeness_from_text=text_data.get('notes_completeness_from_text', []),
        raw_text=text_data.get('raw_lines', []),
        schedule_section_positions=text_data.get('schedule_section_positions', {}),
        section_view_positions=section_view_pos,
        cut_letters=cut_letters,
    )

    if title_data:
        result['title_block'] = title_data.get('title_block') or {}
        result['notes']       = title_data.get('notes') or {}

    if schedule_data:
        raw_sched = schedule_data.get('schedule') or []
        for row in raw_sched:
            comp = (row.get('component') or 'unknown').lower()
            bm   = (row.get('bar_mark') or '').strip().lower()
            if not bm:
                continue
            comp_dict = result['schedule'].setdefault(comp, {})
            if bm in comp_dict:
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

        # shape_dims merged from same call — no separate shape_dims_data
        for comp, bars in (schedule_data.get('shape_dims') or {}).items():
            comp = comp.lower()
            for bm, dims in (bars or {}).items():
                bm = bm.strip().lower()
                bar_row = result['schedule'].get(comp, {}).get(bm)
                if bar_row and isinstance(bar_row, dict) and isinstance(dims, list) and dims:
                    bar_row['shape_dimensions'] = dims

    if review_data:
        result['label_issues']       = review_data.get('label_issues')       or []
        result['dimension_issues']   = review_data.get('dimension_issues')   or []
        result['cross_section_checks'] = review_data.get('cross_section_checks') or []
        result['erroneous_boxes']    = review_data.get('erroneous_boxes')    or []
        # Merge any Claude-reported unlabeled views with text-derived ones
        result['unlabeled_views']    = review_data.get('unlabeled_views')    or []

    # Fill title block / notes gaps from pdfplumber text
    for key, val in text_data.get('title_block', {}).items():
        if not result['title_block'].get(key):
            result['title_block'][key] = val
    for key, val in text_data.get('notes', {}).items():
        if not result['notes'].get(key):
            result['notes'][key] = val

    return result


def run_review_vision(pdf_bytes: bytes, section_labels: list,
                      missing_referenced_sections: list,
                      images_b64: list = None,
                      scale: float = 2.0,
                      judgment_rules: list = None) -> dict | None:
    """
    Run the visual review pass (CHECK 3–7) against the PDF image.
    Returns raw review_data dict from Claude, or None if API key absent / call fails.
    section_labels: list of label strings (e.g. keys of section_view_positions).
    missing_referenced_sections: list of dicts from _text_missing_sections().
    images_b64: pre-rendered images to avoid double-rendering (PDF path passes these).
    scale: render scale when images_b64 is not provided (DXF path uses 1.5× to save memory).
    judgment_rules: pre-filtered list of knowledge_rules.Rule (rule_type='judgment') for
        CHECK 7 — already narrowed by knowledge_rules.get_judgment_rules() to what's
        applicable to this specific drawing (type + entities present). None/empty means
        CHECK 7 renders as "no additional rules to check" rather than an error — the
        pure-PDF-vision path (extract_from_drawing) doesn't have structured drawing_data
        available yet at the point it calls this function, so it doesn't pass any; only
        the DXF path (ed_checker/__init__.py's _run_dxf_extraction, where drawing_data is
        already assembled) currently supplies them.
    """
    label_block = '\n'.join(f'  • {l}' for l in section_labels) or '  (none extracted)'
    unresolved_block = (
        '\n'.join(
            f'  • Cut letter "{m["cut_letter"]}" → {m["missing_section"]} not found'
            for m in (missing_referenced_sections or [])
        ) or '  (none — all cut letters resolved)'
    )
    if judgment_rules:
        knowledge_block = '\n'.join(
            f'  • [{r.rule_id}] {r.reasoning_prompt.strip()} '
            f'(Source: {r.source_reference})'
            for r in judgment_rules
        )
    else:
        knowledge_block = '  (no additional knowledge-base rules apply to this drawing)'
    review_prompt = REVIEW_PROMPT_TEMPLATE.format(
        SECTION_LABELS=label_block,
        UNRESOLVED_CUTS=unresolved_block,
        KNOWLEDGE_RULES=knowledge_block,
    )
    imgs = images_b64 if images_b64 is not None else _pdf_to_image_b64(pdf_bytes, scale=scale)
    return _call_vision(imgs, review_prompt, REVIEW_MODEL, 8192)


ENGINEERING_REVIEW_PROMPT_TEMPLATE = """Act as an experienced structural design reviewer. Use engineering principles, IS/IRC code intent, detailing best practices, and construction knowledge to evaluate not only explicit rule violations but also questionable engineering decisions, inconsistencies, unusual detailing, missing information, constructability concerns, and potential design risks in this bridge foundation drawing (Pile-Pilecap-Pier).

You are reviewing AFTER deterministic code-compliance checks and mechanical drawing-quality checks have already run — do not repeat their job. Your job is holistic engineering judgement: does this design decision make sense, is this detailing pattern typical or unusual, does the visible arrangement match what the schedule/summary below says, would an experienced reviewing engineer raise an eyebrow at anything here even if nothing is numerically wrong.

STRUCTURED SUMMARY (facts already extracted from this drawing — geometry, schedule, notes, and issues other checks already found):
{STRUCTURED_SUMMARY}

ENGINEERING KNOWLEDGE (background context for your reasoning — not a checklist to apply mechanically):
{ENGINEERING_CONCEPTS}

For each observation, explicitly distinguish your confidence:
- "definite": a clear-cut technical problem you are certain about (e.g. a described relationship in the knowledge above is directly violated, not just numerically but in substance).
- "probable": something that looks off or inconsistent and very likely needs correction, but you are not fully certain without more context.
- "needs_verification": worth an experienced engineer's attention, but you are genuinely uncertain — could be a legitimate design choice or could be an error.

Do not invent facts not shown in the summary or the image. Do not flag something already listed in "ALREADY-FLAGGED ISSUES". If you have no genuine observation, return an empty list — do not manufacture findings to have something to say.

Return ONLY valid JSON (no markdown):
{{
  "reasoning_findings": [
    {{"title": "Pile count appears low for pier size shown", "description": "...", "confidence": "needs_verification", "bbox": {{"x":15,"y":30,"w":20,"h":15}}}}
  ]
}}
"""


def run_engineering_review(images_b64: list, structured_summary: str,
                            concepts: list, drawing_type: str) -> dict | None:
    """
    Run the holistic Engineering Reasoning Reviewer pass — feeds the drawing image
    plus a structured summary (geometry/schedule/notes/already-found issues) and
    retrieved engineering-reasoning concepts to Claude, asking it to reason like an
    experienced reviewing engineer rather than check a specific rule. Must be called
    AFTER comparator.compare() and knowledge_rules.evaluate_all_deterministic() have
    both run (see ed_checker/__init__.py's run_check()) — this pass needs their
    output as context, unlike CHECK 1-7 which runs earlier in the pipeline.
    Returns raw review_data dict (`{"reasoning_findings": [...]}`), or None on
    failure/no API key. images_b64 should be the SAME rendered page images already
    produced for the CHECK 1-7 pass — no extra PDF render needed.
    """
    if concepts:
        concepts_block = '\n\n'.join(
            f'  [{c.concept_id}] {c.title}\n  {c.body.strip()}\n  (Source: {c.source_reference})'
            for c in concepts
        )
    else:
        concepts_block = '  (no engineering-reasoning concepts apply to this drawing type/components)'

    prompt = ENGINEERING_REVIEW_PROMPT_TEMPLATE.format(
        STRUCTURED_SUMMARY=structured_summary,
        ENGINEERING_CONCEPTS=concepts_block,
    )
    return _call_vision(images_b64, prompt, ENGINEERING_REVIEW_MODEL, 8192)


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

    schedule_section_positions, section_view_positions, cut_letters = {}, {}, set()
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if pdf.pages:
                schedule_section_positions, section_view_positions, cut_letters = \
                    _extract_positions(pdf.pages[0])
    except Exception as e:
        log.warning('_extract_positions failed: %s', e)

    sections_from_text         = _sections_from_text(section_view_positions)
    notes_completeness_from_text = _notes_completeness_from_text(raw_lines)
    all_label_text             = sorted(section_view_positions.keys())

    return {
        'title_block':                   title_block,
        'notes':                         notes,
        'raw_lines':                     raw_lines,
        'schedule_section_positions':    schedule_section_positions,
        'section_view_positions':        section_view_positions,
        'cut_letters':                   cut_letters,
        'sections_from_text':            sections_from_text,
        'notes_completeness_from_text':  notes_completeness_from_text,
        'all_label_text':                all_label_text,
    }


def _extract_text_with_timeout(pdf_bytes: bytes, timeout: float = 120.0) -> dict:
    """
    Wraps _extract_text with a hard wall-clock budget. pdfplumber's word/char
    clustering (extract_words(), called from here and from _extract_positions)
    can pathologically hang for minutes on some AutoCAD-exported PDFs with
    unusually dense vector geometry — confirmed in production: DXF extraction
    completed in seconds, but the subsequent pdfplumber pass never returned,
    silently blocking the whole review forever (no exception, no further log
    output, worker otherwise still responsive since it's one background
    thread). pdfplumber's output here is supplementary (PDF-coordinate marker
    positions, redundant text fields already covered by DXF) — never
    load-bearing — so a timeout is treated exactly like the existing
    try/except around this call: give up and continue with DXF-only results.
    Python threads can't be forcibly killed, so the abandoned pdfplumber call
    keeps running in the background after a timeout — accepted trade-off over
    blocking the whole review indefinitely.
    """
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_extract_text, pdf_bytes)
    try:
        return future.result(timeout=timeout)
    except _FutureTimeoutError:
        log.warning('pdfplumber _extract_text exceeded %.0fs — giving up, continuing without it',
                     timeout)
        return {}
    finally:
        executor.shutdown(wait=False)


# ── Text-analysis helpers ─────────────────────────────────────────────────────

def _sections_from_text(section_view_positions: dict) -> list:
    """Return presence status for each required PPP section using pdfplumber label positions."""
    all_labels = ' '.join(section_view_positions.keys()).upper()
    result = []
    for name, keywords in _REQUIRED_PPP_SECTIONS:
        present = any(kw.upper() in all_labels for kw in keywords)
        bbox = None
        if present:
            for label, pos in section_view_positions.items():
                if any(kw.upper() in label for kw in keywords):
                    bbox = pos
                    break
        result.append({'name': name, 'present': present, 'bbox': bbox})
    return result


def _notes_completeness_from_text(raw_lines: list) -> list:
    """Return presence status for each required note item using keyword scan of raw text."""
    text = '\n'.join(raw_lines).upper()
    # Normalise non-ASCII hyphens (AutoCAD often writes soft hyphen / en-dash for "Fe-500")
    text = text.replace('\xad', '-').replace('–', '-').replace('—', '-')
    # Concrete keys share the same grade keywords — if any grade found, all three are covered.
    concrete_keys = ('concrete_pile', 'concrete_pilecap', 'concrete_pier')
    concrete_found = any(kw.upper() in text for kw in _NOTE_KEYWORDS.get('concrete_pile', []))
    result = []
    for item_key, keywords in _NOTE_KEYWORDS.items():
        if item_key in concrete_keys:
            present = concrete_found
        else:
            present = any(kw.upper() in text for kw in keywords)
        result.append({'item': item_key, 'present': present, 'value': None})
    return result


def _text_missing_sections(cut_letters: set, section_view_positions: dict) -> list:
    """Cross-reference cut-mark letters against section label text. Returns missing list."""
    # Find which letters are resolved by a SECTION X-X label in the drawing.
    # Row-grouping can merge two adjacent section labels into one combined key
    # (e.g. "SECTION A-A FOR PILE ... SECTION B-B FOR ..." as a single string) —
    # re.findall (not re.search, which stops after the first match) is required
    # so the second label's letter isn't silently dropped.
    found_section_letters: set = set()
    for label in section_view_positions.keys():
        for letter in re.findall(r'SECTION\s+([A-Z])-\1', label.upper()):
            found_section_letters.add(letter)

    missing = []
    for letter in sorted(cut_letters - found_section_letters):
        missing.append({
            'cut_letter': letter,
            'found_on_view': 'drawing (cut marks detected in PDF text)',
            'missing_section': f'SECTION {letter}-{letter}',
            'bbox': None,
        })
    return missing


# ── pdfplumber position extraction ───────────────────────────────────────────


def _extract_positions(page) -> tuple:
    """
    Extract schedule section header positions and section view label positions
    from pdfplumber word bounding boxes (PDF point coordinates → percentages).
    Returns (schedule_section_positions, section_view_positions).
    """
    pw, ph = page.width, page.height
    words = page.extract_words() or []
    # Views on the left, schedule on the right — same layout split as the DXF path
    schedule_x_min = pw * PPP_PROFILE.layout.views_x_max_frac

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
            # Only consider words in the schedule body (above 80% = excludes title block).
            # Take the topmost occurrence — AutoCAD PDFs store text in drawing order,
            # not top-to-bottom, so the first word in stream order may be a false positive
            # from the title block drawn later in the PDF stream.
            if text == keyword and w['top'] < ph * 0.80:
                if comp not in comp_header_y or w['top'] < comp_header_y[comp]:
                    comp_header_y[comp] = w['top']

    sorted_comps = sorted(comp_header_y.items(), key=lambda kv: kv[1])
    sect_x0 = min(schedule_words_x) if schedule_words_x else schedule_x_min

    schedule_section_positions = {}
    for idx, (comp, header_y) in enumerate(sorted_comps):
        y_end = sorted_comps[idx + 1][1] if idx + 1 < len(sorted_comps) else ph * 0.82
        schedule_section_positions[comp] = to_pct(sect_x0, header_y, pw, y_end)

    # ── 2. Find section view labels on the left side ─────────────────────────
    line_words: dict = {}
    single_letter_counts: dict = {}
    single_letter_tops:   dict = {}  # letter → list of 'top' y-values (for axis-label filter)
    for w in words:
        if w['x0'] >= schedule_x_min:
            continue
        line_y = round(w['top'] / 3) * 3
        line_words.setdefault(line_y, []).append(w)
        # Track isolated single uppercase letters for cut-mark detection
        t = w['text'].strip()
        if len(t) == 1 and t.isupper() and t.isalpha():
            single_letter_counts[t] = single_letter_counts.get(t, 0) + 1
            single_letter_tops.setdefault(t, []).append(w['top'])

    # Cut-mark letters appear in PAIRS (at both ends of a cut line).
    # Extra filter: if a letter appears ≥ 3 times all within a 5%-height horizontal
    # band, it is a plan-view axis/grid label (e.g., pier-position labels "C", "D"
    # repeated at each pier column across the plan) — NOT a section cut mark pair.
    # Cut marks must span the sectioned component and therefore appear at meaningfully
    # different y positions (or occur at most twice on the same horizontal cut line).
    _y_band = ph * 0.05
    cut_letters = set()
    for letter, count in single_letter_counts.items():
        if count < 2:
            continue
        tops = single_letter_tops[letter]
        if count >= 3 and (max(tops) - min(tops)) <= _y_band:
            continue  # axis label, not a cut mark
        cut_letters.add(letter)

    section_view_positions = {}
    for line_y, lw in sorted(line_words.items()):
        lw_sorted = sorted(lw, key=lambda x: x['x0'])
        line_text = ' '.join(w['text'] for w in lw_sorted).upper().strip()
        # Normalise non-standard hyphens (soft hyphen U+00AD, en-dash, em-dash) to ASCII '-'
        # so regex and keyword matching work regardless of how the CAD app encoded the label.
        line_text = line_text.replace('\xad', '-').replace('–', '-').replace('—', '-')
        if any(t in line_text for t in TRIGGER_WORDS):
            x0  = min(w['x0']     for w in lw_sorted)
            x1  = max(w['x1']     for w in lw_sorted)
            top = min(w['top']    for w in lw_sorted)
            view_h_pts = ph * 0.18
            name = line_text[:60]
            section_view_positions[name] = to_pct(x0, top, x1, top + view_h_pts)

    # The 'NOTES' heading is excluded from the loop above by the schedule_x_min gate
    # whenever it's drawn in the right/schedule portion of a full combined sheet (a real
    # CASAD layout — confirmed in production) rather than the left/views portion the gate
    # assumes. Give it its own unrestricted, single-purpose scan instead of loosening the
    # shared gate for every TRIGGER_WORDS label, which would risk new false matches for
    # SECTION/TABLE-1/LAP/DETAIL/PLAN/REINFORCEMENT (e.g. the schedule's own "SCHEDULE OF
    # REINFORCEMENT" header) — this keeps the blast radius limited to the Notes bbox only.
    if not any('NOTES' in name for name in section_view_positions):
        notes_word = next(
            (w for w in words
             if w['text'].strip().upper().replace('\xad', '-').startswith('NOTES')
             and w['top'] < ph * 0.80),
            None,
        )
        if notes_word:
            nx0, ntop = notes_word['x0'], notes_word['top']
            section_view_positions[f'NOTES:{round(ntop)}'] = to_pct(
                nx0, ntop, pw, min(ntop + ph * 0.15, ph * 0.82)
            )

    log.info(
        '_extract_positions: schedule_sections=%s section_views=%d cut_letters=%s',
        list(schedule_section_positions.keys()),
        len(section_view_positions),
        sorted(cut_letters),
    )
    return schedule_section_positions, section_view_positions, cut_letters


# ── Claude vision pass ────────────────────────────────────────────────────────

def _pdf_to_image_b64(pdf_bytes: bytes, scale: float = 1.0,
                       crop_right_pct: float = None,
                       clip_rect_pct: tuple = None) -> list:
    """Render each PDF page to a PNG and return list of base64 strings.

    crop_right_pct: keep the rightmost fraction of the page (e.g. 0.40 = right 40%).
    clip_rect_pct:  (x0, y0, x1, y1) as fractions 0–1 of page dimensions — arbitrary crop.
      Takes priority over crop_right_pct when both are supplied.
    """
    try:
        import fitz  # PyMuPDF
        log.info('PyMuPDF rendering PDF (%d bytes) scale=%.1f', len(pdf_bytes), scale)
        doc = fitz.open(stream=pdf_bytes, filetype='pdf')
        images = []
        for i, page in enumerate(doc):
            mat = fitz.Matrix(scale, scale)
            pw, ph = page.rect.width, page.rect.height

            if clip_rect_pct:
                x0f, y0f, x1f, y1f = clip_rect_pct
                clip = fitz.Rect(pw * x0f, ph * y0f, pw * x1f, ph * y1f)
                pix = page.get_pixmap(matrix=mat, clip=clip)
                log.info('Page %d clip (%.0f%%,%.0f%%)→(%.0f%%,%.0f%%): %dx%d px',
                         i+1, x0f*100, y0f*100, x1f*100, y1f*100, pix.width, pix.height)
            elif crop_right_pct and 0 < crop_right_pct < 1.0:
                clip = fitz.Rect(pw * (1.0 - crop_right_pct), 0, pw, ph)
                pix = page.get_pixmap(matrix=mat, clip=clip)
                log.info('Page %d right %.0f%%: %dx%d px',
                         i+1, crop_right_pct * 100, pix.width, pix.height)
            else:
                pix = page.get_pixmap(matrix=mat)
                log.info('Page %d full: %dx%d px', i+1, pix.width, pix.height)

            png_bytes = pix.tobytes('png')
            pix = None  # release pixmap before base64 expansion
            b64 = base64.standard_b64encode(png_bytes).decode()
            png_bytes = None  # release PNG bytes after encoding
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
                  max_tokens: int = 8192, max_images: int = 1) -> dict | None:
    """Send pre-rendered page images + prompt to Claude. Returns parsed JSON or None."""
    if not ANTHROPIC_API_KEY or not images_b64:
        return None
    raw = ''
    try:
        import anthropic
        # Explicit bound instead of the SDK default (~10 min) — a stalled call here
        # would otherwise silently block the whole background thread indefinitely,
        # with no exception for _run_check_bg's except clause to catch.
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=180.0)
        images_to_send = images_b64[:max_images]
        log.info('Vision call: model=%s sending %d image(s), prompt length=%d chars',
                 model, len(images_to_send), len(prompt))

        content = [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': b64}}
            for b64 in images_to_send
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

        # Extract JSON robustly — Claude sometimes adds preamble text or wraps in a code block.
        code_block = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', raw)
        if code_block:
            raw = code_block.group(1).strip()
        elif not raw.startswith(('{', '[')):
            # Skip any preamble and start from the first JSON object or array
            m = re.search(r'[{\[]', raw)
            if m:
                raw = raw[m.start():]

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
