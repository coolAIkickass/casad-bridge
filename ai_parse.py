# ai_parse.py — Claude API: field notes → structured JSON
import json, os
import anthropic

client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

SYSTEM_PROMPT = '''
You are a structural engineering report assistant for CASAD Consultants.
Convert informal field notes into a structured JSON object matching the CASAD bridge inspection report format.
Notes may be in mixed Hindi/English or fragmented.
Output ONLY valid JSON — no markdown, no explanation, no preamble.

Default values for missing information:
- "Not Visible" for foundation observation fields
- "Absent" for superstructure / substructure defect fields
- "-" for miscellaneous / other fields
- "NA" for bearing fields when not applicable
- "NIL" for recommendation fields where no issue was found

Recommendation fields (rec_gen_* and rec_str_*):
- Write each as a complete professional sentence.
- Format: state the problem/deficiency observed, then state the recommended remedial action.
- Example: "Damage observed in wearing coat surface. — Renewal of wearing coat recommended."
- For structural fields, include the specific component (e.g., girder, pier cap) and the defect type.
- For rec_irc_action, write the full IRC SP:40-2019 recommended action sentence for the given condition rating.
  Example for Fair: "Structural deficiency is major. Medium to specialized repair and NDT of various structural components is needed."
'''

SCHEMA = {
    # Section A — Bridge Details
    "river_name":           "",
    "road_name":            "",
    "chainage":             "",
    "latitude":             "",
    "longitude":            "",
    "circle":               "",
    "division":             "",
    "sub_division":         "",
    "no_of_spans":          "",
    "span_length":          "",
    "bridge_type":          "",
    "superstructure_type":  "",
    "substructure_type":    "",
    "foundation_type":      "",
    "bearing_type_detail":  "",
    "total_length":         "",
    "approach_length":      "",
    "railing_type":         "",
    "river_training":       "",
    "repair_work":          "",
    "carriageway_width":    "",
    "year_of_construction": "",
    "bridge_level_type":    "",
    "river_perennial":      "",

    # Section B — Survey
    "date_of_survey": "",

    # B2 — Superstructure
    "ss_cracks":             "",
    "ss_leaching":           "",
    "ss_honey_combing":      "",
    "ss_exposed_rebar":      "",
    "ss_leakage_patches":    "",
    "ss_spalling":           "",
    "ss_rust_marks":         "",
    "ss_shuttering_defects": "",
    "ss_delamination":       "",
    "ss_other":              "",

    # B3 — Substructure
    "sub_cracks":             "",
    "sub_leaching":           "",
    "sub_honey_combing":      "",
    "sub_exposed_rebar":      "",
    "sub_spalling":           "",
    "sub_rust_marks":         "",
    "sub_shuttering_defects": "",
    "sub_delamination":       "",
    "sub_tilting":            "",
    "sub_other":              "",

    # B4 — Foundations
    "found_cracks":             "",
    "found_leaching":           "",
    "found_honey_combing":      "",
    "found_exposed_rebar":      "",
    "found_spalling":           "",
    "found_rust_marks":         "",
    "found_shuttering_defects": "",
    "found_delamination":       "",
    "found_settlement":         "",
    "found_tilting":            "",
    "found_scour":              "",
    "found_other":              "",

    # B5 — Bearings
    "bearing_displacement": "",
    "bearing_distortion":   "",
    "bearing_corrosion":    "",

    # B6 — Approach
    "approach_settlement": "",
    "approach_erosion":    "",
    "approach_other":      "",

    # B7–B9
    "expansion_joint": "",
    "wearing_coat":    "",
    "flood_gauge":     "",
    "masonry_steps":   "",
    "vegetation":      "",

    # Section C — Condition
    "condition_state": "",

    # Section C — Recommendations: General (Non-Structural)
    # Format each field as: "Problem observed. — Recommended action."
    # Use "NIL" if no issue found for that element.
    "rec_gen_training":   "",   # Training & Protection Work
    "rec_gen_wearing":    "",   # Wearing Coat
    "rec_gen_vegetation": "",   # Vegetation Growth
    "rec_gen_expansion":  "",   # Expansion Joint
    "rec_gen_masonry":    "",   # Masonry Steps
    "rec_gen_flood":      "",   # Flood Gauge Mark
    "rec_gen_other":      "",   # Any other general issue

    # Section C — Recommendations: Structural Elements
    # Format: "Defect details observed. — Remedial measure required."
    "rec_str_superstructure": "",   # Girders, deck slab, diaphragm
    "rec_str_substructure":   "",   # Pier, abutment, return wall, pier cap
    "rec_str_bearings":       "",   # Bearings
    "rec_str_foundation":     "",   # Foundation
    "rec_str_other":          "",   # Any other structural issue

    # IRC SP: 40-2019 Rating
    "rec_irc_condition": "",   # Single word: Excellent / Good / Fair / Poor / Critical
    "rec_irc_action":    "",   # Full recommended action sentence per IRC SP:40-2019

    # Signature
    "sign_date":           "",
    "representative_name": "",

    # Photos (list of file paths — appended by report_gen.py)
    "photos": []
}


def parse_inspection(session: dict) -> dict:
    """Send session messages to Claude and return structured JSON."""
    messages_text = '\n'.join(
        m['content'] for m in session.get('messages', []) if m.get('content')
    )
    photo_data = [
        (m['media_path'], m.get('content') or '')
        for m in session.get('messages', [])
        if m.get('media_path')
    ]
    photo_paths    = [p for p, _ in photo_data]
    photo_captions = [c for _, c in photo_data]

    print(f"PARSE: {len(session.get('messages', []))} messages, text length={len(messages_text)}, photos={len(photo_paths)}")
    print(f"FIELD NOTES:\n{messages_text}")

    user_content = (
        f"Schema:\n{json.dumps(SCHEMA, indent=2)}\n\n"
        f"Field notes:\n{messages_text}\n\n"
        f"Photo file paths (in sequence order):\n{json.dumps(photo_paths)}"
    )

    response = client.messages.create(
        model='claude-sonnet-4-5',
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{'role': 'user', 'content': user_content}]
    )
    raw = response.content[0].text.strip()
    print(f"CLAUDE RAW RESPONSE (first 200 chars): {raw[:200]}")

    # Strip markdown code fences if Claude wrapped the JSON
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]
        raw = raw.strip()

    if not raw:
        print("ERROR: Claude returned empty response")
        raise ValueError("Claude returned empty response — check field notes content")

    result = json.loads(raw)
    result['photos']         = photo_paths
    result['photo_captions'] = photo_captions
    print(f"PHOTOS injected: {photo_paths}")
    print(f"CAPTIONS injected: {photo_captions}")
    return result
