"""
ED Checker — main entry point.

Public API:
  parse_design_inputs(design_files)                        -> (design_data dict, parse_errors list)
  run_check(drawing_pdf_bytes, design_data, dxf_bytes)     -> (issues list, detected_type str)
"""
import os
import re
import logging
from .excel_parser import parse_e2e_excel
from .pdf_extractor import extract_from_drawing, _extract_text_with_timeout as _pdf_extract_text
from .pdf_extractor import _text_missing_sections, run_review_vision
from .pdf_extractor import _pdf_to_image_b64, run_engineering_review
from .comparator import compare
from .profiles import DISPLAY_NAME_TO_PROFILE_NAME
from . import engineering_review
from . import knowledge_rules
from ._memutil import trim_memory

log = logging.getLogger(__name__)

ACCEPTED_EXCEL = ('.xlsx', '.xls')
ACCEPTED_IMAGE = ('.jpg', '.jpeg', '.png')
ACCEPTED_PDF   = ('.pdf',)


def parse_design_inputs(design_files: list) -> tuple:
    """
    Parse a list of (filename, bytes) design input files.
    Returns (design_data dict, parse_errors list).
    design_data is JSON-serialisable and can be stored in the DB for reuse on re-uploads.
    """
    design_data = {}
    parse_errors = []

    for fname, fbytes in (design_files or []):
        ext = _ext(fname)
        if ext in ACCEPTED_EXCEL:
            try:
                parsed = parse_e2e_excel(fbytes)
                for k, v in parsed.items():
                    if v:
                        design_data[k] = v
            except Exception as e:
                parse_errors.append(f'{fname}: {e}')
                log.warning('Design input parse error — %s: %s', fname, e)
        # Reference PDFs / JPEGs: not parsed in this version

    return design_data, parse_errors


def detect_drawing_type(drawing_data: dict) -> str:
    """Infer drawing type from extracted drawing content."""
    schedule   = drawing_data.get('schedule', {})
    sections   = drawing_data.get('sections_from_text', []) or []
    section_names = ' '.join((s.get('name') or '') for s in sections if s.get('present')).upper()
    view_labels   = ' '.join(drawing_data.get('section_view_positions', {}) or {}).upper()
    title      = (drawing_data.get('title_block', {}).get('title') or '').upper()

    all_text = section_names + ' ' + view_labels + ' ' + title

    if 'pile' in schedule or 'pilecap' in schedule or 'PILE' in all_text:
        return 'Pile Pilecap Pier'
    if 'ABUTMENT' in all_text:
        return 'Abutment'
    if 'SUPERSTRUCTURE' in all_text or 'GIRDER' in all_text or 'DECK' in all_text:
        return 'Superstructure'
    if 'BEARING' in all_text:
        return 'Bearing'
    return 'General'


def run_check(drawing_pdf_bytes: bytes, design_data: dict,
              dxf_bytes: bytes = None) -> tuple:
    """
    drawing_pdf_bytes : raw bytes of the drawing PDF (required — used for display)
    design_data       : already-parsed design data dict (from parse_design_inputs)
                        pass {} or None if no design input was provided
    dxf_bytes         : optional AutoCAD DXF bytes; when provided, exact DXF extraction
                        replaces Claude vision for schedule, title block, notes, TABLE-1,
                        and cross-section bar counting

    Returns (issues list, detected_type str).
    """
    if dxf_bytes:
        drawing_data, vision_ran = _run_dxf_extraction(drawing_pdf_bytes, dxf_bytes)
    else:
        drawing_data = extract_from_drawing(drawing_pdf_bytes)
        api_key_missing = not os.environ.get('ANTHROPIC_API_KEY', '').strip()
        vision_ran = bool(drawing_data.get('schedule'))

        if not vision_ran:
            log.warning(
                'Vision extraction returned no schedule data. '
                'api_key_missing=%s raw_text_lines=%d',
                api_key_missing,
                len(drawing_data.get('raw_text', []))
            )

    if dxf_bytes:
        # DXF path: schedule extraction is exact — no vision fallback needed
        if not vision_ran:
            issues = [{
                'category': 'Configuration',
                'title': 'DXF extraction returned no schedule data',
                'description': (
                    'The uploaded DXF file was parsed but no reinforcement schedule was found. '
                    'Ensure the DXF contains the schedule in the right portion of the drawing '
                    'and that text entities are present (not just lines).'
                ),
                'suggestion': 'Check that the DXF was exported with text (not as outlines). '
                              'Try File > Save As > AutoCAD DXF in AutoCAD.',
                'severity': 'error', 'page_num': 1,
                'x': 5, 'y': 5, 'width': 90, 'height': 10,
            }]
            issues += compare(design_data or None, drawing_data)
        else:
            issues = compare(design_data or None, drawing_data)

    elif not os.environ.get('ANTHROPIC_API_KEY', '').strip() and not vision_ran:
        issues = [{
            'category': 'Configuration',
            'title': 'AI vision check not available — ANTHROPIC_API_KEY not set',
            'description': (
                'Schedule tables, TABLE-1 levels, and notes could not be checked because '
                'the AI vision API key is not configured on this server. '
                'Title block format checks ran from text extraction. '
                'Alternatively, upload an AutoCAD DXF file for exact schedule extraction '
                'without needing the vision API.'
            ),
            'suggestion': 'Set ANTHROPIC_API_KEY in the Render service environment variables, '
                          'or upload a DXF file alongside the PDF.',
            'severity': 'warning', 'page_num': 1,
            'x': 5, 'y': 5, 'width': 90, 'height': 10,
        }]
        text_only = compare(design_data or None, drawing_data)
        issues += [i for i in text_only if i.get('category') not in ('Reinforcement', 'Levels (TABLE-1)')]

    elif not vision_ran:
        issues = [{
            'category': 'Configuration',
            'title': 'AI vision extraction failed',
            'description': (
                'The ANTHROPIC_API_KEY is configured but the AI vision check returned no data. '
                'This may be caused by a PDF rendering error (PyMuPDF) or an API timeout. '
                'Alternatively, upload an AutoCAD DXF file for exact schedule extraction.'
            ),
            'suggestion': 'Check Render logs for errors from ed_checker/pdf_extractor.py, '
                          'or upload a DXF file alongside the PDF.',
            'severity': 'warning', 'page_num': 1,
            'x': 5, 'y': 5, 'width': 90, 'height': 10,
        }]
        text_only = compare(design_data or None, drawing_data)
        issues += [i for i in text_only if i.get('category') not in ('Reinforcement', 'Levels (TABLE-1)')]

    else:
        issues = compare(design_data or None, drawing_data)

    detected_type = detect_drawing_type(drawing_data)

    # Knowledge-rule engine (IRC/IS code-compliance checks) — a sibling checker
    # to comparator.compare() above, not a replacement. Needs only drawing_data,
    # never design_data, so it runs in every branch above including when no
    # design Excel was uploaded at all. profile_name maps the human-readable
    # detected_type ("Pile Pilecap Pier") to the short profile identifier
    # ("ppp") that knowledge_rules' applicable_drawing_types tags use.
    profile_name = DISPLAY_NAME_TO_PROFILE_NAME.get(detected_type)
    if profile_name:
        try:
            issues += knowledge_rules.evaluate_all_deterministic(drawing_data, profile_name)
        except Exception:
            log.exception('Knowledge-rule engine failed — continuing without its issues')

    # CHECK 7 judgment-rule findings (DXF path only — see _run_dxf_extraction) were
    # already translated to _issue()-shape by build_judgment_issues() and stashed
    # here; nothing further to interpret, just append.
    issues += drawing_data.get('knowledge_rule_issues') or []

    # Engineering Reasoning Reviewer — holistic "does this design decision make
    # sense" judgement, distinct from CHECK 7's narrow per-clause judgment calls.
    # Must run here, AFTER issues is fully assembled (comparator + deterministic
    # knowledge_rules + CHECK 7 all merged above) — it needs their output as
    # context, unlike CHECK 7 which runs earlier inside _run_dxf_extraction and
    # has no visibility into comparator/deterministic-rule results. Findings land
    # in category='AI Reasoning', rendered in their own tab, not mixed into the
    # severity-driven Issues list — see engineering_review/__init__.py docstring.
    # DXF path only for now (needs the page image _run_dxf_extraction already
    # rendered for CHECK 1-7 — no image available at this point in the PDF-only
    # path); mirrors CHECK 7's own DXF-path-only precedent.
    _page_images = drawing_data.get('_rendered_page_images_b64')
    if profile_name and _page_images and os.environ.get('ANTHROPIC_API_KEY', '').strip():
        try:
            concepts = engineering_review.get_relevant_concepts(profile_name, drawing_data)
            summary  = engineering_review.build_structured_summary(drawing_data, issues)
            review   = run_engineering_review(_page_images, summary, concepts, detected_type)
            issues  += engineering_review.build_reasoning_issues(review)
        except Exception:
            log.exception('Engineering reviewer failed — continuing without it')

    return issues, detected_type


def _run_dxf_extraction(pdf_bytes: bytes, dxf_bytes: bytes) -> tuple:
    """
    Run DXF extraction and merge pdfplumber position data.
    Returns (drawing_data dict, vision_ran bool).
    vision_ran is True when the DXF schedule was non-empty.
    """
    from .dxf_extractor import extract_from_dxf

    drawing_data = extract_from_dxf(dxf_bytes)

    # Always run pdfplumber on the PDF — it gives accurate PDF-coordinate positions
    # for marker placement on the review UI, and supplements completeness checks.
    try:
        text_data = _pdf_extract_text(pdf_bytes)

        # Override schedule_section_positions — these must be PDF coordinates
        drawing_data['schedule_section_positions'] = text_data.get('schedule_section_positions', {})

        # Merge section_view_positions: start with DXF labels, overlay pdfplumber labels.
        # DXF-derived bboxes are percentages of the DXF drawing EXTENTS; pdfplumber-derived
        # bboxes are percentages of the PDF PAGE — two different coordinate spaces that
        # happen to share the same 0-100 range and field names. The two label strings are
        # reconstructed by independent text-grouping logic and frequently don't match
        # character-for-character, so a plain {**dxf_sv, **pdf_sv} union (de-duping only on
        # exact key match) often keeps BOTH a dxf-space and a pdf-space entry for the same
        # physical section under different keys. Tag provenance so downstream coordinate
        # lookups (_xsec_bbox/_find_section_bbox in comparator.py) can filter to pdf-space
        # entries only — using a dxf-space bbox as if it were a pdf-space bbox silently
        # mis-places the marker on the review UI. Keys-only consumers (_text_missing_sections,
        # the view_labels join above, _section_labels below) are unaffected by the added field.
        dxf_sv = drawing_data.get('section_view_positions', {})
        pdf_sv = text_data.get('section_view_positions', {})
        merged_sv = {k: {**v, '_space': 'dxf'} for k, v in dxf_sv.items()}
        merged_sv.update({k: {**v, '_space': 'pdf'} for k, v in pdf_sv.items()})
        drawing_data['section_view_positions'] = merged_sv

        # Prefer pdfplumber sections_from_text but patch any present=False entries
        # using the combined section_view_positions — pdfplumber can miss sections
        # that are text-underlined (%%U codes) or use non-standard PDF encoding.
        if text_data.get('sections_from_text'):
            sft = text_data['sections_from_text']
            # Trust the DXF-only pass's own verdict — TRUE OR FALSE — for every entry
            # it covers. It was computed with profile.required_sections' own keyword
            # tuples (_check_required_sections), which are short, robust, AND
            # position-aware about longer-sibling domination (e.g. distinguishes a
            # single "REINFORCEMENT PLAN OF PILECAP" label from two separate,
            # row-merged "REINFORCEMENT PLAN OF PILECAP" + "PLAN OF PILECAP" views).
            # The proximity patch below re-derives matching from each entry's full
            # display NAME instead (e.g. 'SECTION Z-Z (PILE)', 'LAP LENGTH TABLE')
            # via plain substring containment with no position-awareness at all —
            # confirmed on a production sheet to wrongly flip 'SECTION A-A FOR PILE'
            # to present=True purely because "PILE" is a literal prefix of "PILECAP"
            # inside an unrelated "SECTION A-A FOR PILECAP & ABUTMENT" label, even
            # though the DXF-only pass had already (correctly) rejected it. Since
            # profile.required_sections is a fixed list shared by both extractors,
            # DXF's own pass always has an entry for every name in sft — the
            # proximity-patch fallback below only ever runs for entries outside that
            # shared list, which shouldn't happen in practice but is kept as a
            # defensive fallback.
            dxf_sft = {e['name']: e for e in drawing_data.get('sections_from_text', [])}
            sv_upper = {k.upper() for k in merged_sv}
            for entry in sft:
                if not entry.get('present'):
                    if entry['name'] in dxf_sft:
                        dxf_entry = dxf_sft[entry['name']]
                        if dxf_entry.get('present'):
                            entry['present'] = True
                            entry['bbox'] = dxf_entry.get('bbox') or entry.get('bbox')
                            log.info('sections_from_text: patched %r to present=True via DXF '
                                     '(required_sections keyword match)', entry['name'])
                        continue
                    name_u = entry['name'].upper()
                    # Check if any keyword from this section appears in merged sv keys.
                    # Longest-match guard: skip sv_key if another section's display name
                    # is also a substring of sv_key AND is longer than name_u — that
                    # other section owns the label more specifically (e.g. prevents
                    # "PLAN OF PILECAP" from being satisfied by a label that actually
                    # contains the longer "REINFORCEMENT PLAN OF PILECAP").
                    other_names_u = [e['name'].upper() for e in sft if e['name'].upper() != name_u]
                    for sv_key in sv_upper:
                        if name_u not in sv_key and sv_key not in name_u:
                            continue
                        dominated = any(
                            oname in sv_key and name_u in oname and len(oname) > len(name_u)
                            for oname in other_names_u
                        )
                        if not dominated:
                            entry['present'] = True
                            log.info('sections_from_text: patched %r to present=True via DXF',
                                     entry['name'])
                            break
            drawing_data['sections_from_text'] = sft
        if text_data.get('notes_completeness_from_text'):
            # DXF TEXT entities are exact; pdfplumber can miss a whole note line when
            # CASAD's PDF plot renders an SHX-font note body as vector outlines rather
            # than real text — extract_words() then returns nothing for that line at
            # all (confirmed on a production sheet: pdfplumber found 'NOTES:-' as a
            # heading but zero words containing FE/STEEL/GRADE for the body text below
            # it). This list is structurally always non-empty (one entry per note item,
            # present True or False), so the old unconditional overwrite silently
            # clobbered an already-correct DXF-confirmed 'present: True' with pdfplumber's
            # False. Recover anything the DXF-only pass already confirmed present —
            # same pattern as the sections_from_text patch above.
            dxf_present_items = {
                n['item'] for n in drawing_data.get('notes_completeness_from_text', [])
                if n.get('present')
            }
            pdf_ncft = text_data['notes_completeness_from_text']
            for entry in pdf_ncft:
                if not entry.get('present') and entry.get('item') in dxf_present_items:
                    entry['present'] = True
                    log.info('notes_completeness_from_text: patched %r to present=True via DXF',
                             entry['item'])
            drawing_data['notes_completeness_from_text'] = pdf_ncft

        # Supplement title block and notes gaps from pdfplumber (belt-and-suspenders)
        for key, val in text_data.get('title_block', {}).items():
            if not drawing_data['title_block'].get(key):
                drawing_data['title_block'][key] = val
        for key, val in text_data.get('notes', {}).items():
            if not drawing_data['notes'].get(key):
                drawing_data['notes'][key] = val

        # DXF path: trust DXF cut_letters exclusively when non-empty.
        # pdfplumber is unreliable for AutoCAD PDFs — AutoCAD sometimes stores text
        # with per-character positioning, causing pdfplumber to return individual
        # characters (e.g. 'C' from 'PILECAP', 'D' from 'CHECKED') as separate
        # "words", each counted as a single-letter occurrence. This inflates cut_letter
        # counts, producing false-positive SECTION C-C / SECTION D-D missing-view errors.
        # DXF TEXT entities are whole strings, so DXF cut_letters are reliable.
        # Only fall back to pdfplumber when DXF found zero cut letters.
        if not drawing_data.get('cut_letters') and text_data.get('cut_letters'):
            drawing_data['cut_letters'] = text_data['cut_letters']
        # Do NOT union: pdfplumber false letters must not pollute DXF-detected cut_letters.

    except Exception as e:
        log.warning('pdfplumber merge failed: %s', e)
        drawing_data.setdefault('extraction_diagnostics', []).append({
            'code': 'pdfplumber_merge_failed',
            'message': (
                f'PDF text positions could not be merged ({e}). Issue markers may be '
                f'misplaced on the review viewer and some completeness checks may report '
                f'false missing-view errors.'
            ),
            'severity': 'error',
        })

    # Compute cut-mark cross-reference using merged cut_letters + section_view_positions.
    # Preserve any DXF-derived missing refs (e.g. DETAIL cross-references from
    # _detect_missing_detail_refs) — append text-derived cut-letter refs without overwriting.
    cut_letters  = drawing_data.get('cut_letters', set())
    sv_pos       = drawing_data.get('section_view_positions', {})
    text_missing = _text_missing_sections(cut_letters, sv_pos)
    dxf_missing  = drawing_data.get('missing_referenced_sections') or []
    already      = {m['missing_section'] for m in dxf_missing}
    drawing_data['missing_referenced_sections'] = dxf_missing + [
        m for m in text_missing if m['missing_section'] not in already
    ]

    # Free DXF bytes — no longer needed after extraction; saves 25 MB before vision render.
    dxf_bytes = None
    trim_memory()  # belt-and-suspenders: extract_from_dxf already trimmed, but catch any stragglers

    # Run the visual review pass (CHECK 3–6) even in DXF path.
    # DXF text extraction cannot detect unlabeled views, stray boxes,
    # missing dimensions, or label/annotation quality issues.
    # Use 2.0× render (same as PDF path) — 1.5× was tried to save memory but Claude
    # misses small boxes and unlabeled views at that resolution.
    try:
        _section_labels = sorted(drawing_data.get('section_view_positions', {}).keys())
        _missing_secs   = drawing_data.get('missing_referenced_sections', [])
        # CHECK 7 judgment-rule retrieval — drawing_data is fully assembled by this
        # point, so detect_drawing_type() + the rule engine's applicable_drawing_types/
        # required_entities filtering can run here. (The pure-PDF-vision path in
        # pdf_extractor.py's extract_from_drawing() can't do this — drawing_data isn't
        # built yet at its equivalent call site — so it passes no judgment_rules.)
        _profile_name = DISPLAY_NAME_TO_PROFILE_NAME.get(detect_drawing_type(drawing_data))
        _judgment_rules = (
            knowledge_rules.get_judgment_rules(_profile_name, drawing_data)
            if _profile_name else []
        )
        # Render once, reuse for both this call and the later Engineering Reasoning
        # Reviewer pass (run_check() reuses drawing_data['_rendered_page_images_b64'])
        # instead of each rendering the PDF independently.
        _page_images = _pdf_to_image_b64(pdf_bytes, scale=2.0)
        drawing_data['_rendered_page_images_b64'] = _page_images
        review_data = run_review_vision(pdf_bytes, _section_labels, _missing_secs,
                                        images_b64=_page_images,
                                        judgment_rules=_judgment_rules)
        if review_data:
            log.info('DXF review vision OK — label_issues=%d unlabeled_views=%d '
                     'erroneous_boxes=%d cross_section_checks=%d knowledge_rule_findings=%d',
                     len(review_data.get('label_issues') or []),
                     len(review_data.get('unlabeled_views') or []),
                     len(review_data.get('erroneous_boxes') or []),
                     len(review_data.get('cross_section_checks') or []),
                     len(review_data.get('knowledge_rule_findings') or []))
            drawing_data['label_issues']         = review_data.get('label_issues')         or []
            drawing_data['knowledge_rule_issues'] = knowledge_rules.build_judgment_issues(
                review_data.get('knowledge_rule_findings'), _judgment_rules
            )
            # Merge DXF-detected dimension issues (text-override mismatches, liner
            # thickness units, TABLE-1 duplicate headers — all exact, no vision needed)
            # with vision-detected ones (CHECK 4's absent-only rule) — do not overwrite.
            drawing_data['dimension_issues'] = (
                (drawing_data.get('dimension_issues') or []) +
                (review_data.get('dimension_issues') or [])
            )
            drawing_data['cross_section_checks'] = (
                drawing_data.get('cross_section_checks') or
                review_data.get('cross_section_checks') or []
            )
            drawing_data['erroneous_boxes']      = review_data.get('erroneous_boxes')      or []
            # Merge DXF-detected unlabeled circles with vision-detected unlabeled views.
            _dxf_unlabeled    = drawing_data.get('unlabeled_views') or []
            _vision_unlabeled = review_data.get('unlabeled_views')  or []
            drawing_data['unlabeled_views'] = _dxf_unlabeled + _vision_unlabeled
        else:
            log.warning('DXF path: review vision returned None — visual checks (label quality, '
                        'unlabeled views, erroneous boxes) could not run')
            drawing_data['extraction_diagnostics'].append({
                'code': 'review_vision_failed',
                'message': (
                    'The visual review pass (label quality, unlabeled views, erroneous box '
                    'detection) could not run — the AI vision call returned no data. '
                    'Check Render logs for the underlying error. These checks must be '
                    'performed manually for this drawing.'
                ),
                'severity': 'error',
            })
    except Exception as e:
        log.warning('DXF path: review vision call failed: %s', e)
        drawing_data['extraction_diagnostics'].append({
            'code': 'review_vision_failed',
            'message': (
                f'The visual review pass failed with an error ({e}). '
                'Label quality, unlabeled views, and erroneous box checks could not run. '
                'Check Render logs for details.'
            ),
            'severity': 'error',
        })

    vision_ran = bool(drawing_data.get('schedule'))
    return drawing_data, vision_ran


def _ext(filename: str) -> str:
    return os.path.splitext(filename.lower())[1]
