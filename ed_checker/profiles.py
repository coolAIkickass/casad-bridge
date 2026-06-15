"""
Drawing-type profiles for the ED Checker.

All drawing-type-specific knowledge (component names, schedule sub-header patterns,
required section views, notes keywords/patterns, bar-mark conventions, sheet layout
fractions) lives here as data. The extractors (dxf_extractor, pdf_extractor) and the
comparator consume a profile — adding a new drawing type (Abutment, Superstructure,
Bearing) means adding a new DrawingTypeProfile instance, not editing extractor code.

Currently only the Pile-Pilecap-Pier profile exists (PPP_PROFILE).
"""
import re
from dataclasses import dataclass, field

# Section/view label trigger words — shared by both extractors for label detection.
TRIGGER_WORDS = {'SECTION', 'TABLE-1', 'TABLE 1', 'LAP', 'NOTES', 'DETAIL', 'PLAN', 'REINFORCEMENT'}


@dataclass(frozen=True)
class LayoutConfig:
    """
    Sheet layout assumptions. All *_frac values are fractions of sheet width/height
    (unit-independent); all *_mm values are millimetres and must be divided by the
    drawing's units-to-mm factor before comparison against raw coordinates.
    """
    # Region partitioning
    schedule_x_min_frac: float = 0.30   # schedule occupies the right (1 - this) of the sheet
    views_x_max_frac: float = 0.55      # drawing views occupy the left this-fraction
    title_x_min_frac: float = 0.60      # title block: right of this …
    title_y_max_frac: float = 0.30      # … and below this (of height, from bottom in DXF)
    # Scan windows around anchors
    notes_w_frac: float = 0.40          # half-width of notes scan around the NOTES label
    notes_h_frac: float = 0.40          # height of notes scan below the label
    table1_w_frac: float = 0.30         # half-width of TABLE-1 scan around its label
    table1_h_frac: float = 0.30         # height of TABLE-1 scan below its label
    # Cross-section bar counting
    xsec_dx_frac: float = 0.25          # half-width of the dot search box around a section label
    xsec_below_frac: float = 0.40       # search box extends this far below the label
    xsec_above_frac: float = 0.05       # … and this far above
    dot_max_r_frac: float = 0.05        # max bar-dot radius (filters section boundary circles)
    cluster_gap_frac: float = 0.15      # max gap between dots of one section view cluster
    # Label / row geometry
    view_h_frac: float = 0.18           # estimated section-view height below its label
    header_band_frac: float = 0.012     # Y-band that collects multi-line schedule header rows
    sched_row_tol_frac: float = 0.004   # Y tolerance for schedule row grouping
    row_tol_frac: float = 0.005         # Y tolerance for generic row grouping
    # Absolute thresholds (millimetres — convert via units factor before use)
    table_offset_min_mm: float = 1000.0  # X shift that signals a second side-by-side schedule block


@dataclass(frozen=True)
class DrawingTypeProfile:
    """Everything the pipeline needs to know about one drawing type."""
    name: str                       # machine name, e.g. 'ppp'
    display_name: str               # detected_type string, e.g. 'Pile Pilecap Pier'
    components: tuple               # schedule components in canonical order
    comp_header_patterns: tuple     # ((component, compiled regex), ...) — order matters
                                    # (e.g. PILECAP must be tested before PILE)
    required_sections: tuple        # ((display_name, (keyword, ...)), ...)
    note_keywords: dict             # completeness check: item_key -> [presence keywords]
    note_float_patterns: dict       # value extraction: notes key -> regex (float capture group(s))
    note_string_patterns: dict      # value extraction: notes key -> regex (string capture group)
    concrete_grade_keywords: tuple  # grades that satisfy the concrete_* completeness items
    bar_mark_comp_fallback: dict    # bar mark letter -> component; FALLBACK ONLY — used when the
                                    # schedule has no component sub-header rows at all
    title_patterns: tuple           # substrings identifying the drawing title text
    dot_layer_patterns: tuple       # regexes matched against DXF layer names; entities on
                                    # matching layers are preferred as rebar-dot candidates
    dot_block_patterns: tuple = ()  # regexes matched against block names; matching block
                                    # INSERTs are authoritative rebar-dot symbols
    geometry_checks: tuple = ()     # ((param, design_key, tol_pct, label, design_unit), ...)
                                    # param: key in geometry_from_drawing
                                    # design_key: key in design_data['geometry']
                                    # tol_pct: allowed % difference before flagging
                                    # design_unit: 'm' (multiply ×1000 to get mm) or 'mm'
    layout: LayoutConfig = field(default_factory=LayoutConfig)

    def comps_longest_first(self) -> list:
        """Component names sorted longest-first, for substring matching where one
        component name contains another (PILECAP contains PILE)."""
        return sorted(self.components, key=len, reverse=True)

    def total_row_guard_re(self):
        """Regex matching total-weight summary rows like 'PILE = 11460 KG' that must
        not be mistaken for component sub-headers."""
        comps = '|'.join(sorted((c.upper() for c in self.components), key=len, reverse=True))
        return re.compile(r'\b(' + comps + r')\s*=\s*\d', re.IGNORECASE)


PPP_PROFILE = DrawingTypeProfile(
    name='ppp',
    display_name='Pile Pilecap Pier',
    components=('pilecap', 'pile', 'pier'),
    # PILECAP must be checked before PILE since "PILECAP" contains "PILE".
    comp_header_patterns=(
        ('pilecap', re.compile(r'\bPILECAP\b', re.IGNORECASE)),
        ('pile',    re.compile(r'\bPILE\b(?!CAP)', re.IGNORECASE)),
        ('pier',    re.compile(r'\bPIER\b', re.IGNORECASE)),
    ),
    required_sections=(
        ('SECTION A-A FOR PILE',           ('A-A FOR PILE',)),
        ('SECTION Z-Z (PILE)',             ('Z-Z',)),
        ('SECTION A-A FOR PILECAP & PIER', ('A-A FOR PILECAP',)),
        ('SECTION B-B FOR PILECAP & PIER', ('B-B FOR PILECAP',)),
        ('PLAN OF PILECAP',                ('PLAN OF PILECAP',)),
        ('REINFORCEMENT PLAN OF PILECAP',  ('REINFORCEMENT PLAN',)),
        ('TABLE-1',                        ('TABLE-1', 'TABLE 1')),
        ('LAP LENGTH TABLE',               ('LAP LENGTH',)),
        ('SCHEDULE OF REINFORCEMENT',      ('SCHEDULE OF REINFORCEMENT',)),
    ),
    note_keywords={
        'pile_length':      ['PILE LENGTH', 'LENGTH OF PILE'],
        'pile_fixity':      ['FIXITY', 'FIX. LENGTH', 'FIX LENGTH', 'FIXATION'],
        'pile_diameter':    ['PILE DIA', 'DIAMETER OF PILE', 'PILE DIAMETER'],
        'concrete_pile':    ['M30', 'M35', 'M40', 'M45', 'M50'],
        'concrete_pilecap': ['M30', 'M35', 'M40', 'M45', 'M50'],
        'concrete_pier':    ['M30', 'M35', 'M40', 'M45', 'M50'],
        'steel_grade':      ['FE415', 'FE500', 'FE550', 'FE 415', 'FE 500', 'FE 550', 'HYSD', 'TMT'],
        'irc_code_ref':     ['IRC:', 'IRC-', 'IRC '],
    },
    note_float_patterns={
        'pile_length_m':   r'PILE\s+LENGTH\s*[=:]\s*([\d.]+)\s*M?',
        'pile_fixity_m':   r'FIXITY\s*[=:]\s*([\d.]+)\s*M?|FIX\.?\s*LENGTH\s*[=:]\s*([\d.]+)',
        'pile_dia_m':      r'PILE\s+DIA\.?\s*[=:]\s*([\d.]+)\s*M?',
        'max_pile_load_t': r'MAX\.?\s+(?:SAFE\s+)?PILE\s+LOAD\s*[=:]\s*([\d.]+)\s*T',
    },
    note_string_patterns={
        'steel_grade':               r'\b(Fe\s*\d+|FE\s*\d+|HYSD|TMT)\b',
        'lap_length_concrete_grade': r'LAP\s+LENGTH\s+FOR\s+BAR\s+FOR\s+(M\d+)',
    },
    concrete_grade_keywords=('M30', 'M35', 'M40', 'M45', 'M50'),
    bar_mark_comp_fallback={
        'x': 'pile', 'y': 'pile', 'y1': 'pile', 'z': 'pile',
        'a': 'pilecap', 'b': 'pilecap', 'c': 'pilecap', 'd': 'pilecap',
        'e': 'pilecap', 'f': 'pilecap', 'f1': 'pilecap',
        'g': 'pier', 'h': 'pier', 'h1': 'pier', 'h2': 'pier',
        'i': 'pier', 'i1': 'pier',
        'j': 'pier', 'j1': 'pier',
        'k': 'pier', 'k1': 'pier',
    },
    title_patterns=('DETAILS OF PILE',),
    dot_layer_patterns=(r'REBAR', r'REINF', r'\bBAR\b'),
    # CASAD rebar dots are 'REIN.DOT' / 'REIN. DOT' block inserts
    dot_block_patterns=(r'REIN.{0,2}DOT', r'\bDOT\b', r'REBAR'),
    # Spatial geometric dimension checks — DXF defpoint routing vs design geometry.
    # Each tuple: (param_in_geometry_from_drawing, design_geometry_key, tol_pct, label, design_unit)
    # design_unit 'm' → multiply design value × 1000 before comparing with DXF mm value.
    geometry_checks=(
        ('pilecap_depth',          'pilecap_depth',          2.0, 'Pilecap depth',              'm'),
        ('pile_spacing',           'pile_spacing',           2.0, 'Pile c/c spacing',           'm'),
        ('pile_overhang',          'pile_overhang',          5.0, 'Pile overhang',              'm'),
        ('pilecap_width',          'pilecap_length_across',  2.0, 'Pilecap width (across)',     'm'),
        ('pilecap_length_overall', 'pilecap_length_along',   2.0, 'Pilecap length (along)',     'm'),
        ('pile_dia',               'pile_dia',               2.0, 'Pile diameter',              'm'),
    ),
)
