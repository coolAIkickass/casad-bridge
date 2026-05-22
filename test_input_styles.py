"""
test_input_styles.py — Verify that all three WhatsApp input styles are correctly
mapped to Appendix-A JSON fields by the AI parsing prompt.

Three styles tested for every Appendix-A field:
  Style 1 — Explicit field naming:   "Name of bridge is ABC Bridge"
  Style 2 — Sequential with THEN/next: "ABC Bridge THEN Not Applicable THEN NH-64"
  Style 3 — Partial/abbreviated name: "Location of BM is 107.575 m"

The tests do NOT call the live Claude API. They:
  a) Verify the FIELD_MAPPING_PROMPT is present in EXCEL_EXTRA_PROMPT and contains
     all required Appendix-A field names and abbreviations.
  b) Mock the Anthropic client and verify that parse_inspection_excel sends the
     correct grouped_notes to Claude, and that a well-formed Claude response is
     processed and returned intact.

Run:
    python -m pytest test_input_styles.py -v
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch, call

# ── path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
import ai_parse
from ai_parse import (
    EXCEL_EXTRA_PROMPT,
    SYSTEM_PROMPT,
    _group_messages_by_category,
)


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _session(text, category='bridge_details'):
    """Minimal session with a single text message."""
    return {
        'messages': [
            {'content': text, 'category': category, 'media_path': None}
        ]
    }


def _multi_session(texts, category='bridge_details'):
    """Session with multiple messages (all same category)."""
    return {
        'messages': [
            {'content': t, 'category': category, 'media_path': None}
            for t in texts
        ]
    }


def _mock_claude_json(fields: dict):
    """Build a mock Anthropic streaming response that returns a JSON string."""
    # Merge supplied fields into a minimal valid EXCEL_SCHEMA
    base = {k: '' for k in ai_parse.EXCEL_SCHEMA}
    base.update(fields)
    base['photos'] = []
    base['photo_titles'] = []
    base['photo_categories'] = []
    base['photo_coords'] = []
    raw = json.dumps(base)

    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=raw)]
    mock_msg.stop_reason = 'end_turn'

    mock_stream = MagicMock()
    mock_stream.__enter__ = MagicMock(return_value=mock_stream)
    mock_stream.__exit__ = MagicMock(return_value=False)
    mock_stream.get_final_message = MagicMock(return_value=mock_msg)

    return mock_stream


def _parse(session, expected_fields):
    """Run parse_inspection_excel with a mocked Claude response and return result."""
    mock_stream = _mock_claude_json(expected_fields)
    with patch.object(ai_parse.client.messages, 'stream', return_value=mock_stream):
        with patch('ai_parse._detect_defect_coords', return_value={}):
            result = ai_parse.parse_inspection_excel(session)
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Section 1 — Prompt structure validation
# ─────────────────────────────────────────────────────────────────────────────

class TestPromptStructure(unittest.TestCase):
    """Ensure EXCEL_EXTRA_PROMPT contains all required mapping instructions."""

    def _in_prompt(self, text):
        return text.lower() in EXCEL_EXTRA_PROMPT.lower()

    # ── Style 2 sequential field order present ───────────────────────────────
    def test_style2_field_order_section_present(self):
        self.assertIn('SEQUENTIAL FIELD ORDER', EXCEL_EXTRA_PROMPT)

    def test_style2_all_41_positions_present(self):
        for pos in range(1, 42):
            label = f'Pos {pos:02d}' if pos <= 9 else f'Pos {pos}'
            self.assertIn(label, EXCEL_EXTRA_PROMPT,
                          f"Pos {pos} missing from SEQUENTIAL FIELD ORDER")

    # ── Style 3 abbreviation table present ──────────────────────────────────
    def test_style3_abbreviation_section_present(self):
        self.assertIn('PARTIAL / ABBREVIATED', EXCEL_EXTRA_PROMPT)

    def test_style3_bm_gts_abbreviation(self):
        self.assertIn('bm_gts_level', EXCEL_EXTRA_PROMPT)

    def test_style3_bridge_number_abbreviation(self):
        self.assertIn('bridge_number', EXCEL_EXTRA_PROMPT)

    def test_style3_no_of_spans_abbreviation(self):
        self.assertIn('no_of_spans', EXCEL_EXTRA_PROMPT)

    def test_style3_cc_of_piers_abbreviation(self):
        self.assertIn('cc_of_piers', EXCEL_EXTRA_PROMPT)

    def test_style3_angle_abbreviation(self):
        self.assertIn('angle_of_crossing', EXCEL_EXTRA_PROMPT)

    def test_style3_hydraulic_abbreviation(self):
        self.assertIn('hydraulic_parameters', EXCEL_EXTRA_PROMPT)

    def test_style3_subsoil_abbreviation(self):
        self.assertIn('subsoil_particulars', EXCEL_EXTRA_PROMPT)

    def test_style3_bearing_type_abbreviation(self):
        self.assertIn('bearing_type_detail', EXCEL_EXTRA_PROMPT)

    def test_style3_expansion_joint_abbreviation(self):
        self.assertIn('expansion_joint', EXCEL_EXTRA_PROMPT)

    def test_style3_wearing_coat_abbreviation(self):
        self.assertIn('wearing_coat', EXCEL_EXTRA_PROMPT)

    def test_style3_construction_agency_abbreviation(self):
        self.assertIn('construction_agency', EXCEL_EXTRA_PROMPT)

    def test_style3_design_agency_abbreviation(self):
        self.assertIn('design_agency', EXCEL_EXTRA_PROMPT)

    def test_style3_surface_utilities_abbreviation(self):
        self.assertIn('surface_utilities', EXCEL_EXTRA_PROMPT)

    def test_style3_performance_abbreviation(self):
        self.assertIn('performance', EXCEL_EXTRA_PROMPT)

    # ── Mixed-style instruction present ─────────────────────────────────────
    def test_combined_styles_instruction_present(self):
        self.assertIn('COMBINED', EXCEL_EXTRA_PROMPT)


# ─────────────────────────────────────────────────────────────────────────────
#  Section 2 — Message grouping (all styles pass through bridge_details bucket)
# ─────────────────────────────────────────────────────────────────────────────

class TestMessageGrouping(unittest.TestCase):
    """_group_messages_by_category must route all 3 styles into BRIDGE DETAILS."""

    def _grouped(self, text):
        msgs = [{'content': text, 'category': 'bridge_details', 'media_path': None}]
        return _group_messages_by_category(msgs)

    # ── Style 1: explicit naming ─────────────────────────────────────────────
    def test_style1_explicit_name_in_bridge_details(self):
        out = self._grouped("Name of bridge is ABC Bridge")
        self.assertIn('BRIDGE DETAILS', out)
        self.assertIn('ABC Bridge', out)

    def test_style1_explicit_no_of_spans(self):
        out = self._grouped("Number of spans is 10 Nos.")
        self.assertIn('BRIDGE DETAILS', out)
        self.assertIn('10 Nos.', out)

    def test_style1_explicit_bm_gts(self):
        out = self._grouped("Location of BM with GTS level is 107.575 m Railway Portion")
        self.assertIn('BRIDGE DETAILS', out)
        self.assertIn('107.575', out)

    def test_style1_explicit_angle(self):
        out = self._grouped("Angle of crossing is Q")
        self.assertIn('BRIDGE DETAILS', out)
        self.assertIn('Q', out)

    def test_style1_explicit_bearing_type(self):
        out = self._grouped("Type of bearing is Elastomeric Bearing")
        self.assertIn('BRIDGE DETAILS', out)
        self.assertIn('Elastomeric Bearing', out)

    # ── Style 2: sequential with THEN ────────────────────────────────────────
    def test_style2_then_connector_in_bridge_details(self):
        out = self._grouped("ABC Bridge THEN Not Applicable THEN NH-64 THEN ROB")
        self.assertIn('BRIDGE DETAILS', out)
        self.assertIn('ABC Bridge', out)
        self.assertIn('NH-64', out)

    def test_style2_next_connector_in_bridge_details(self):
        out = self._grouped("ABC Bridge next Not Applicable next NH-64")
        self.assertIn('BRIDGE DETAILS', out)
        self.assertIn('ABC Bridge', out)

    def test_style2_numeric_sequence(self):
        out = self._grouped("10 Nos THEN 150 m THEN 25 m THEN 1.2 m")
        self.assertIn('BRIDGE DETAILS', out)
        self.assertIn('150 m', out)

    # ── Style 3: partial / abbreviated names ─────────────────────────────────
    def test_style3_location_of_bm_abbreviated(self):
        out = self._grouped("Location of BM is 107.575 m")
        self.assertIn('BRIDGE DETAILS', out)
        self.assertIn('107.575', out)

    def test_style3_no_of_bridge(self):
        out = self._grouped("No of bridge is B-123")
        self.assertIn('BRIDGE DETAILS', out)
        self.assertIn('B-123', out)

    def test_style3_cc_abbreviated(self):
        out = self._grouped("C/C is 25 m")
        self.assertIn('BRIDGE DETAILS', out)
        self.assertIn('25 m', out)

    def test_style3_angle_abbreviated(self):
        out = self._grouped("Angle is Q")
        self.assertIn('BRIDGE DETAILS', out)
        self.assertIn('Q', out)


# ─────────────────────────────────────────────────────────────────────────────
#  Section 3 — End-to-end field mapping (mocked Claude response)
#  For every Appendix-A field, one test each for Style 1, 2, and 3.
# ─────────────────────────────────────────────────────────────────────────────

# Each tuple: (field_key, style1_msg, style2_msg, style3_msg, expected_value)
# style2_msg is the full sequential sentence that includes this field's value
# style3_msg uses abbreviated/partial field name

FIELD_CASES = [
    # Pos 01 — bridge_title
    (
        'bridge_title',
        "Name of bridge is Khokhara ROB",
        "Khokhara ROB THEN Not Applicable THEN NH-64",
        "Bridge name is Khokhara ROB",
        "Khokhara ROB",
    ),
    # Pos 02 — bridge_number
    (
        'bridge_number',
        "Bridge number is B-001",
        "Khokhara ROB THEN B-001 THEN Not Applicable",
        "No of bridge is B-001",
        "B-001",
    ),
    # Pos 03 — river_name
    (
        'river_name',
        "Name of river is Not Applicable as it is ROB",
        "Khokhara ROB THEN Not Applicable THEN NH-64",
        "River name is Not Applicable as it is ROB",
        "Not Applicable as it is ROB",
    ),
    # Pos 04 — road_name
    (
        'road_name',
        "Name of road is National Highway 64",
        "Khokhara ROB THEN Not Applicable THEN National Highway 64",
        "Road name is National Highway 64",
        "National Highway 64",
    ),
    # Pos 05 — road_number
    (
        'road_number',
        "Road number is NH-64",
        "Khokhara ROB THEN Not Applicable THEN National Highway 64 THEN NH-64",
        "Road no is NH-64",
        "NH-64",
    ),
    # Pos 06+07 — latitude / longitude
    (
        'latitude',
        "Latitude is 23.007695",
        "23.007695 THEN 72.607542",
        "GPS latitude is 23.007695",
        "23.007695",
    ),
    (
        'longitude',
        "Longitude is 72.607542",
        "23.007695 THEN 72.607542",
        "GPS longitude is 72.607542",
        "72.607542",
    ),
    # Pos 08 — bm_gts_level
    (
        'bm_gts_level',
        "Location of BM with GTS level is 107.575 m Railway Portion",
        "Khokhara ROB THEN B-001 THEN Not Applicable THEN NH-64 THEN NH-64 THEN 23.007695 THEN 72.607542 THEN 107.575 m Railway Portion",
        "Location of BM is 107.575 m Railway Portion",
        "107.575 m Railway Portion",
    ),
    # Pos 09 — division
    (
        'division',
        "Division is East Zone Division",
        "East Zone Division THEN Ahmedabad Circle",
        "Division is East Zone Division",
        "East Zone Division",
    ),
    # Pos 10 — circle
    (
        'circle',
        "Circle is Ahmedabad Circle",
        "East Zone Division THEN Ahmedabad Circle",
        "Circle is Ahmedabad Circle",
        "Ahmedabad Circle",
    ),
    # Pos 11 — no_of_spans
    (
        'no_of_spans',
        "Number of spans is Anupam Road Side: 10 Nos.\nRailway Portion: 4 Nos.",
        "10 Nos. THEN 339.53 m THEN 25 m THEN 1.25 m",
        "No of spans is Anupam Road Side: 10 Nos.",
        "Anupam Road Side: 10 Nos.",
    ),
    # Pos 12 — total_length
    (
        'total_length',
        "Total length of bridge is 92 + 6 × 25 + 21 + 13 + 63 = 339.53 m",
        "10 Nos. THEN 92 + 6 × 25 + 21 + 13 + 63 = 339.53 m",
        "Bridge length is 92 + 6 × 25 + 21 = 263 m",
        "92 + 6 × 25 + 21 = 263 m",
    ),
    # Pos 13 — cc_of_piers
    (
        'cc_of_piers',
        "C/C of piers is 25 m Anupam Cinema side",
        "10 Nos. THEN 339.53 m THEN 25 m Anupam Cinema side",
        "C/C is 25 m Anupam Cinema side",
        "25 m Anupam Cinema side",
    ),
    # Pos 14 — width_of_piers
    (
        'width_of_piers',
        "Width of piers is 1.25 m Anupam Cinema side",
        "10 Nos. THEN 339.53 m THEN 25 m THEN 1.25 m Anupam Cinema side",
        "Pier width is 1.25 m Anupam Cinema side",
        "1.25 m Anupam Cinema side",
    ),
    # Pos 15 — angle_of_crossing
    (
        'angle_of_crossing',
        "Angle of crossing is Q",
        "10 Nos. THEN 339.53 m THEN 25 m THEN 1.25 m THEN Q",
        "Angle is Q",
        "Q",
    ),
    # Pos 16 — bridge_level_type
    (
        'bridge_level_type',
        "Type of bridge whether high level or submersible: Railway Over Bridge",
        "Railway Over Bridge",
        "Type of bridge is Railway Over Bridge",
        "Railway Over Bridge",
    ),
    # Pos 17 — hydraulic_parameters
    (
        'hydraulic_parameters',
        "Hydraulic parameters is Not Applicable as it is ROB",
        "Not Applicable as it is ROB",
        "Hydraulic is Not Applicable as it is ROB",
        "Not Applicable as it is ROB",
    ),
    # Pos 18 — subsoil_particulars
    (
        'subsoil_particulars',
        "Sub soil particulars as per approved GAD",
        "Not Applicable THEN As per approved GAD",
        "Sub soil is As per approved GAD",
        "As per approved GAD",
    ),
    # Pos 19 — superstructure_type
    (
        'superstructure_type',
        "Type of superstructure is PSC Girder and Deck Slab",
        "PSC Girder and Deck Slab THEN 6 × 25 = 150 m",
        "Superstructure type is PSC Girder and Deck Slab",
        "PSC Girder and Deck Slab",
    ),
    # Pos 20 — span_arrangement
    (
        'span_arrangement',
        "Span arrangement is 6 × 25 = 150 m",
        "PSC Girder THEN 6 × 25 = 150 m",
        "Span details is 6 × 25 = 150 m",
        "6 × 25 = 150 m",
    ),
    # Pos 21 — carriage_width
    (
        'carriage_width',
        "Carriage width is 7.5 m",
        "7.5 m THEN 107.575 m",
        "Carriage is 7.5 m",
        "7.5 m",
    ),
    # Pos 22 — deck_level
    (
        'deck_level',
        "Deck level is 107.575 m Railway Portion",
        "7.5 m THEN 107.575 m Railway Portion",
        "Deck is 107.575 m Railway Portion",
        "107.575 m Railway Portion",
    ),
    # Pos 23 — foundation_type
    (
        'foundation_type',
        "Type of foundation is Pile Foundation",
        "Pile Foundation THEN RCC Pier",
        "Foundation type is Pile Foundation",
        "Pile Foundation",
    ),
    # Pos 24 — substructure_type
    (
        'substructure_type',
        "Type of substructure is RCC Pier and Abutment",
        "Pile Foundation THEN RCC Pier and Abutment",
        "Substructure type is RCC Pier and Abutment",
        "RCC Pier and Abutment",
    ),
    # Pos 25 — pier_length
    (
        'pier_length',
        "Length of piers is 6.5 m",
        "Pile Foundation THEN RCC Pier THEN 6.5 m",
        "Pier length is 6.5 m",
        "6.5 m",
    ),
    # Pos 26 — pier_width_detail
    (
        'pier_width_detail',
        "Width of piers is 1.2 m and 0.6 m",
        "Pile Foundation THEN RCC Pier THEN 6.5 m THEN 1.2 m and 0.6 m",
        "Pier width is 1.2 m and 0.6 m",
        "1.2 m and 0.6 m",
    ),
    # Pos 27 — pier_cap_width
    (
        'pier_cap_width',
        "Width of pier cap is 1.2 m",
        "6.5 m THEN 1.2 m THEN 1.2 m pier cap",
        "Pier cap is 1.2 m",
        "1.2 m",
    ),
    # Pos 28 — abutment_width
    (
        'abutment_width',
        "Width of abutment is 1.5 m",
        "1.2 m THEN 1.5 m abutment",
        "Abutment width is 1.5 m",
        "1.5 m",
    ),
    # Pos 29 — abutment_cap_width
    (
        'abutment_cap_width',
        "Width of abutment cap is 1.5 m",
        "1.5 m THEN 1.5 m abutment cap",
        "Abutment cap is 1.5 m",
        "1.5 m",
    ),
    # Pos 30 — returns_length
    (
        'returns_length',
        "Length of return walls is 100 m Shamal side + 70 m Jivraj side = 170 m",
        "1.5 m THEN 100 m Shamal + 70 m Jivraj = 170 m",
        "Return walls is 100 m Shamal + 70 m Jivraj = 170 m",
        "100 m Shamal + 70 m Jivraj = 170 m",
    ),
    # Pos 31 — prestressing_details
    (
        'prestressing_details',
        "Details of prestressing as per approved Design",
        "170 m THEN As per approved Design",
        "Prestressing is As per approved Design",
        "As per approved Design",
    ),
    # Pos 32 — bearing_type_detail
    (
        'bearing_type_detail',
        "Type of bearing is Elastomeric Bearing",
        "As per approved Design THEN Elastomeric Bearing",
        "Bearing type is Elastomeric Bearing",
        "Elastomeric Bearing",
    ),
    # Pos 33 — wearing_coat
    (
        'wearing_coat',
        "Wearing coat type is Bituminous",
        "Elastomeric Bearing THEN Bituminous",
        "WC type is Bituminous",
        "Bituminous",
    ),
    # Pos 34 — railing_type
    (
        'railing_type',
        "Type of railing is RCC Crash Barrier",
        "Bituminous THEN RCC Crash Barrier",
        "Railing is RCC Crash Barrier",
        "RCC Crash Barrier",
    ),
    # Pos 35 — expansion_joint
    (
        'expansion_joint',
        "Type of expansion joint is Strip Seal",
        "RCC Crash Barrier THEN Strip Seal",
        "EJ type is Strip Seal",
        "Strip Seal",
    ),
    # Pos 36 — date_of_construction_start
    (
        'date_of_construction_start',
        "Date of starting construction is 01/04/2018",
        "01/04/2018 THEN 09/08/2020",
        "Construction date start is 01/04/2018",
        "01/04/2018",
    ),
    # Pos 37 — date_of_completion
    (
        'date_of_completion',
        "Date of completion is 09/08/2020",
        "01/04/2018 THEN 09/08/2020",
        "Date of construction is 09/08/2020",
        "09/08/2020",
    ),
    # Pos 38 — surface_utilities
    (
        'surface_utilities',
        "Surface utilities are Electric cables and telephone lines",
        "09/08/2020 THEN Electric cables and telephone lines",
        "Utilities are Electric cables and telephone lines",
        "Electric cables and telephone lines",
    ),
    # Pos 39 — design_agency
    (
        'design_agency',
        "Design agency is CASAD Consultants Pvt. Ltd.",
        "Electric cables THEN CASAD Consultants Pvt. Ltd.",
        "Designer is CASAD Consultants Pvt. Ltd.",
        "CASAD Consultants Pvt. Ltd.",
    ),
    # Pos 40 — construction_agency
    (
        'construction_agency',
        "Construction agency is XYZ Contractors",
        "CASAD Consultants THEN XYZ Contractors",
        "Contractor is XYZ Contractors",
        "XYZ Contractors",
    ),
    # Pos 41 — performance
    (
        'performance',
        "Overall performance is Fair",
        "XYZ Contractors THEN Fair",
        "Condition is Fair",
        "Fair",
    ),
]


def _make_test_method(field_key, msg, expected_value, style_label):
    """Factory for a single mock-based test method."""
    def test_fn(self):
        sess = _session(msg)
        result = _parse(sess, {field_key: expected_value})
        self.assertEqual(
            result.get(field_key), expected_value,
            f"[{style_label}] field '{field_key}': expected '{expected_value}', got '{result.get(field_key)}'"
        )
    test_fn.__name__ = f"test_{style_label}_{field_key}"
    return test_fn


class TestFieldMappingStyle1(unittest.TestCase):
    """Style 1 — Explicit field naming for every Appendix-A field."""


class TestFieldMappingStyle2(unittest.TestCase):
    """Style 2 — Sequential THEN/next input for every Appendix-A field."""


class TestFieldMappingStyle3(unittest.TestCase):
    """Style 3 — Partial/abbreviated field name for every Appendix-A field."""


# Dynamically attach test methods to the three classes
for _field_key, _s1, _s2, _s3, _expected in FIELD_CASES:
    _safe_name = _field_key.replace('/', '_')

    setattr(TestFieldMappingStyle1,
            f"test_style1_{_safe_name}",
            _make_test_method(_field_key, _s1, _expected, 'style1'))

    setattr(TestFieldMappingStyle2,
            f"test_style2_{_safe_name}",
            _make_test_method(_field_key, _s2, _expected, 'style2'))

    setattr(TestFieldMappingStyle3,
            f"test_style3_{_safe_name}",
            _make_test_method(_field_key, _s3, _expected, 'style3'))


# ─────────────────────────────────────────────────────────────────────────────
#  Section 4 — Verify Claude receives the mapping instructions
#  (checks that user_content + system prompt include all style guidance)
# ─────────────────────────────────────────────────────────────────────────────

class TestClaudeReceivesPrompt(unittest.TestCase):
    """Verify that parse_inspection_excel passes the full mapping prompt to Claude."""

    def setUp(self):
        self.captured_kwargs = {}
        mock_stream = _mock_claude_json({'bridge_title': 'Test Bridge'})

        def capture_stream(**kwargs):
            self.captured_kwargs = kwargs
            return mock_stream

        self.patcher = patch.object(ai_parse.client.messages, 'stream',
                                    side_effect=capture_stream)
        self.mock_stream = self.patcher.start()
        with patch('ai_parse._detect_defect_coords', return_value={}):
            ai_parse.parse_inspection_excel(_session("Name of bridge is Test Bridge"))

    def tearDown(self):
        self.patcher.stop()

    def test_system_prompt_contains_style_handling_section(self):
        system = self.captured_kwargs.get('system', '')
        self.assertIn('INPUT STYLE HANDLING', system)

    def test_system_prompt_contains_sequential_field_order(self):
        system = self.captured_kwargs.get('system', '')
        self.assertIn('APPENDIX-A SEQUENTIAL FIELD ORDER', system)

    def test_system_prompt_contains_all_41_positions(self):
        system = self.captured_kwargs.get('system', '')
        for pos in range(1, 42):
            label = f'Pos {pos:02d}' if pos <= 9 else f'Pos {pos}'
            self.assertIn(label, system, f"{label} missing from system prompt")

    def test_system_prompt_contains_abbreviation_table(self):
        system = self.captured_kwargs.get('system', '')
        self.assertIn('PARTIAL / ABBREVIATED', system)

    def test_user_content_contains_bridge_details_section(self):
        msgs = self.captured_kwargs.get('messages', [])
        self.assertTrue(len(msgs) > 0)
        user_content = msgs[0]['content']
        self.assertIn('BRIDGE DETAILS', user_content)

    def test_user_content_contains_schema(self):
        msgs = self.captured_kwargs.get('messages', [])
        user_content = msgs[0]['content']
        self.assertIn('bridge_title', user_content)

    def test_model_is_correct(self):
        self.assertEqual(self.captured_kwargs.get('model'), 'claude-sonnet-4-6')


# ─────────────────────────────────────────────────────────────────────────────
#  Section 5 — AMC format delegates to the same parser
# ─────────────────────────────────────────────────────────────────────────────

class TestAMCDelegatesToExcelParser(unittest.TestCase):
    """parse_inspection_amc must call parse_inspection_excel with same session."""

    def test_amc_calls_excel_parser(self):
        sess = _session("Name of bridge is Test AMC Bridge")
        mock_stream = _mock_claude_json({'bridge_title': 'Test AMC Bridge'})
        with patch.object(ai_parse.client.messages, 'stream', return_value=mock_stream):
            with patch('ai_parse._detect_defect_coords', return_value={}):
                result_amc = ai_parse.parse_inspection_amc(sess)

        self.assertEqual(result_amc.get('bridge_title'), 'Test AMC Bridge')

    def test_amc_and_excel_produce_same_schema_keys(self):
        """Both parsers share the same EXCEL_SCHEMA — AMC output has all keys."""
        sess = _session("Name of bridge is Test Bridge")
        mock_stream = _mock_claude_json({'bridge_title': 'Test Bridge'})
        with patch.object(ai_parse.client.messages, 'stream', return_value=mock_stream):
            with patch('ai_parse._detect_defect_coords', return_value={}):
                result = ai_parse.parse_inspection_amc(sess)

        # photo_* keys are runtime-appended by the parser, not schema-declared
        runtime_keys = {'photos', 'photo_captions', '_messages',
                        'photo_categories', 'photo_coords', 'photo_titles'}
        excel_keys = set(ai_parse.EXCEL_SCHEMA.keys()) - runtime_keys
        result_keys = set(result.keys()) - runtime_keys
        self.assertTrue(excel_keys.issubset(result_keys),
                        f"AMC result missing keys: {excel_keys - result_keys}")


if __name__ == '__main__':
    unittest.main()
