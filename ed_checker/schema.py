"""
Shared drawing_data contract for the ED Checker.

Both extractors (dxf_extractor.extract_from_dxf, pdf_extractor.extract_from_drawing)
must produce this exact dict shape so comparator.compare() works unchanged on either.
This module is the single source of truth — do not hand-build drawing_data dicts in
the extractors; call new_drawing_data(**overrides) instead.
"""


# What an extraction path can vouch for. The comparator consults these instead of
# branching on where the data came from (replaces the old per-bar 'from_dxf' flag).
# Defaults are the PDF/vision path's capabilities; the DXF path overrides per drawing
# (e.g. 'spacing' is only True when the schedule actually has a C/C column).
DEFAULT_CAPABILITIES = {
    'spacing':          True,   # schedule has a c/c spacing column the extractor can read
    'shape_dims':       True,   # bar shape sketch dimensions are extracted
    'visual_bar_count': True,   # cross-section bar dots can be counted
    'label_review':     True,   # label/spelling review available
    'dimension_review': True,   # dimension-completeness review available
}


def new_drawing_data(**overrides) -> dict:
    """
    Return a complete drawing_data dict with every key the comparator and the
    review UI expect. Pass extracted values as keyword overrides; anything not
    supplied keeps its empty default.
    """
    data = {
        # Core extracted content
        'schedule':                     {},     # comp -> {bar_mark -> bar dict}
        'title_block':                  {},
        'notes':                        {},
        'dim_data':                     {},     # DXF DIMENSION-derived data (DXF path only)
        # Visual / completeness checks
        'cross_section_checks':         [],
        'label_issues':                 [],
        'dimension_issues':             [],
        'unlabeled_views':              [],
        'erroneous_boxes':              [],
        'missing_referenced_sections':  [],
        'sections_from_text':           [],
        'notes_completeness_from_text': [],
        # Position data (PDF coordinates, for review-UI marker placement)
        'schedule_section_positions':   {},
        'schedule_section_bboxes':      {},
        'section_view_positions':       {},
        'cut_letters':                  set(),
        # Coordinate calibration (DXF path only)
        # DXF-extent-% y positions of PILECAP/PILE/PIER header rows.
        # Paired with schedule_section_positions (PDF-%) to compute y-axis
        # linear transform so row_bbox coords map correctly to the PDF viewer.
        'dxf_comp_anchors':             {},
        # Provenance & diagnostics
        'capabilities':                 dict(DEFAULT_CAPABILITIES),
        'extraction_diagnostics':       [],     # [{code, message, severity}] — see diag()
        'raw_text':                     [],
        # Geometric dimension checking (DXF spatial routing — Tier 2)
        'geometry_from_drawing':        {},     # param → [{val_mm, x_pct, y_pct, component, source}, ...]
        'multileader_callouts':         [],     # [{bar_mark, x_pct, y_pct}] from MULTILEADER entities
    }
    unknown = set(overrides) - set(data)
    if unknown:
        raise KeyError(f'new_drawing_data: unknown drawing_data keys {sorted(unknown)}')
    data.update(overrides)
    return data


def diag(code: str, message: str, severity: str = 'error') -> dict:
    """
    Build one extraction diagnostic.
    severity 'error' — surfaced to the engineer as a review issue (something could
                       not be checked and they must know).
    severity 'info'  — recorded for the debug route only (fallbacks used, counts).
    """
    return {'code': code, 'message': message, 'severity': severity}
