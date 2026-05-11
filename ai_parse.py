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

CRITICAL — Structural component accuracy:
- Always use the EXACT component name the inspector mentioned. Do not generalise, substitute, or infer.
  Examples:
  • Inspector says "diaphragm" → use "diaphragm", NOT "superstructure"
  • Inspector says "superstructure" → use "superstructure", NOT "pier cap" or "substructure"
  • Inspector says "pier cap" → use "pier cap", NOT "substructure"
  • Inspector says "abutment" → use "abutment", NOT "substructure"
- Map the defect to the JSON field that matches the inspector's stated component:
  • Superstructure components (girder, deck slab, diaphragm, soffit) → ss_* fields
  • Substructure components (pier, abutment, pier cap, return wall) → sub_* fields
  • Foundation components (pile, pile cap, well) → found_* fields
- NEVER move a defect to a different structural section than the one the inspector stated.
- If the same defect note is about both superstructure and substructure, fill both sections.

CRITICAL — Photo titles (photo_titles field):
- Use the inspector's EXACT component and defect words from the photo description.
- Do NOT rephrase, generalise, or substitute component names.
- Format: "[Defect type] in [exact component name as stated]"
  Examples:
  • Description "honeycombing and leaching in diaphragm" → "Honeycombing and Leaching in Diaphragm"
  • Description "leaching superstructure" → "Leaching in Superstructure"
  • Description "crack pier cap" → "Crack in Pier Cap"

Recommendation fields (rec_gen_* and rec_str_*):
- Write each as a complete professional sentence.
- Format: state the problem/deficiency observed, then state the recommended remedial action.
- Example: "Damage observed in wearing coat surface. — Renewal of wearing coat recommended."
- Use the inspector's exact component name — do not substitute (e.g. write "diaphragm", not "superstructure").
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
    "photos": [],

    # Photo metadata — parallel lists, one entry per photo (appended by report_gen.py)
    "photo_titles":     [],   # short title (max 10 words) per photo
    "photo_categories": [],   # "general" or "damage" per photo
}


def _find_photo_description(messages: list, photo_idx: int) -> str:
    """Return the best description for a photo.

    Priority:
      1. Inline WhatsApp caption on the photo itself
      2. Nearest text/voice note in the 3 messages BEFORE the photo
      3. Nearest text/voice note in the 3 messages AFTER the photo
    """
    m = messages[photo_idx]
    # 1. Inline caption
    if m.get('content'):
        return m['content']

    def _is_text(msg):
        content = (msg.get('content') or '').strip()
        return bool(content) and not msg.get('media_path') and 'done' not in content.lower()

    # 2. Look back up to 3 messages
    for k in range(photo_idx - 1, max(photo_idx - 4, -1), -1):
        if _is_text(messages[k]):
            return messages[k]['content'].strip()

    # 3. Look forward up to 3 messages (voice/text sent after the photo)
    for k in range(photo_idx + 1, min(photo_idx + 4, len(messages))):
        if _is_text(messages[k]):
            return messages[k]['content'].strip()

    return ''


def parse_inspection(session: dict) -> dict:
    """Send session messages to Claude and return structured JSON."""
    messages = session.get('messages', [])
    messages_text = '\n'.join(m['content'] for m in messages if m.get('content'))

    # Collect photo paths and their best available description
    photo_paths        = []
    photo_captions     = []   # raw description (used for circle detection)
    photo_descriptions = []   # same, passed to Claude for title generation

    for j, m in enumerate(messages):
        if m.get('media_path'):
            desc = _find_photo_description(messages, j)
            photo_paths.append(m['media_path'])
            photo_captions.append(desc)
            photo_descriptions.append(desc)

    print(f"PARSE: {len(messages)} messages, text length={len(messages_text)}, photos={len(photo_paths)}")
    print(f"FIELD NOTES:\n{messages_text}")
    print(f"PHOTO DESCRIPTIONS: {photo_descriptions}")

    user_content = (
        f"Schema:\n{json.dumps(SCHEMA, indent=2)}\n\n"
        f"Field notes:\n{messages_text}\n\n"
        f"Photo file paths (in sequence order):\n{json.dumps(photo_paths)}\n\n"
        f"Photo descriptions (one per photo, same order):\n{json.dumps(photo_descriptions)}\n\n"
        "For the photo_titles field: generate a short title (max 10 words) for each photo "
        "based on its description. If no description, infer from field notes context. "
        "photo_titles must have exactly the same number of entries as photo file paths.\n\n"
        "For the photo_categories field: classify each photo as 'general' or 'damage'. "
        "Default to 'damage' for all photos UNLESS: (a) the user explicitly says 'general', "
        "'site photo', 'overview', 'approach', 'zoomed out', or similar; OR (b) the description "
        "clearly describes a wide/panoramic shot of the whole bridge or site with no specific defect. "
        "Any photo mentioning a crack, spalling, leaching, corrosion, exposed rebar, scour, "
        "settlement, distress, or damage must be 'damage'. When in doubt, use 'damage'. "
        "photo_categories must have exactly the same number of entries as photo file paths."
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

    # Ensure photo_titles has the right count; fall back to first 10 words of description
    titles = result.get('photo_titles') or []
    if len(titles) != len(photo_paths):
        titles = [' '.join(d.split()[:10]) if d else '' for d in photo_descriptions]
    result['photo_titles'] = titles

    # Ensure photo_categories has the right count; fall back to 'damage'
    cats = result.get('photo_categories') or []
    if len(cats) != len(photo_paths):
        cats = ['damage'] * len(photo_paths)
    result['photo_categories'] = cats

    print(f"PHOTOS injected: {photo_paths}")
    print(f"TITLES: {result['photo_titles']}")
    print(f"CATEGORIES: {result['photo_categories']}")
    return result
