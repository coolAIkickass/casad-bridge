# ai_parse.py — Claude API: field notes → structured JSON
import json, os
import anthropic
from mark_image import mark_defect

client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

SYSTEM_PROMPT = '''
You are a structural engineering report assistant for CASAD Consultants with deep knowledge of bridge components.
Convert informal field notes into a structured JSON object matching the CASAD bridge inspection report format.
Notes may be in mixed Hindi/English or fragmented.
Output ONLY valid JSON — no markdown, no explanation, no preamble.

BRIDGE COMPONENT KNOWLEDGE — use this to correctly classify and map observations:

SUPERSTRUCTURE components (map to ss_* fields):
- Girders / I-beams: main longitudinal load-bearing members running the length of the span
- Deck slab: flat top surface of the bridge that vehicles drive on
- Diaphragm: vertical cross-members connecting girders laterally, perpendicular to span
- Soffit: underside (ceiling) of the deck slab between girders
- Wearing coat: the road surface layer on top of the deck slab
- Expansion joint: the gap/seal between deck sections allowing thermal movement
- Parapet / railing / crash barrier: safety barriers along bridge edges
- Bracings: cross-members providing lateral stiffness between girders
- Watermarks / leakage patches: water stains visible on soffit or girder faces

SUBSTRUCTURE components (map to sub_* fields):
- Pier / column: vertical support columns rising from ground or water to support the span
- Pier cap / coping: horizontal beam on top of the pier directly under girder ends
- Abutment: end support wall at each end of the bridge where it meets the road embankment
- Return wall / wing wall: angled walls extending from the abutment retaining the earth fill
- Crash barrier (at pier base): vehicle impact protection thickening at pier base in urban areas

FOUNDATION components (map to found_* fields):
- Pile: deep structural elements driven into the ground to transfer load
- Pile cap: thick concrete slab connecting tops of piles
- Well foundation: large-diameter caisson sunk into riverbed
- Open foundation / footing: shallow spread footing at base of piers
Note: foundation components are typically NOT VISIBLE during visual inspection — default to "Not Visible"

BEARINGS (map to bearing_* fields):
- Elastomeric bearing pad: rubber pad between girder end and pier cap allowing movement
- Roller / rocker bearing: metallic bearing allowing rotation and translation
- Bearing pedestal: concrete block hosting the bearing on top of pier cap
Key defects: displacement (lateral shift), distortion (bulging/deformation), corrosion (rust on metallic bearings)

APPROACH (map to approach_* fields):
- Approach slab / road: the road section immediately before/after the bridge
- Slope protection: erosion protection on embankment sides near bridge ends
Key defects: settlement (uneven sinking), erosion of slope

DEFECT CLASSIFICATION GUIDE:
- Cracks: visible lines/gaps in concrete (note length, width, location)
- Leaching / efflorescence: white calcium carbonate deposits streaking down concrete surfaces
- Honeycombing: rough porous concrete with visible voids — poor compaction during casting
- Exposed reinforcement: steel rebar bars visible through broken/spalled concrete
- Spalling: concrete chunks broken away leaving rough exposed surface
- Rust marks: reddish-brown iron oxide staining on concrete from corroding rebar
- Delamination: concrete separating in layers (sounds hollow when tapped)
- Tilting: visible lean/rotation of substructure elements
- Settlement: sinking/subsidence of foundation or approach
- Scour: erosion of riverbed material around foundation (observed at waterline)

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

CRITICAL — Photo number references in observation fields:
- When filling ss_*, sub_*, found_*, bearing_*, approach_*, expansion_joint, wearing_coat, vegetation fields:
  - If a defect is observed, append the relevant photo numbers: "Observed (Photo No.-1), (Photo No.-3)"
  - Match photo numbers using the Photo information list provided — reference photos whose description mentions that specific defect and component
  - Only add photo references when the value is "Observed" or a specific description — NOT for "Absent", "Not Visible", "NA", "NIL", "-"
  - Format exactly: "Observed (Photo No.-X)" or "Observed (Photo No.-X), (Photo No.-Y)"

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


def _find_photo_description(messages: list, photo_idx: int,
                             claimed: set = None) -> str:
    """Return the best description for a photo, marking the source as claimed.

    Priority:
      1. Inline WhatsApp caption on the photo itself
      2. Text/voice in the SAME section (same category) that explicitly
         references this photo number (e.g. "photo 2 ...")
      3. Nearest unclaimed text/voice in the SAME section, up to 3 before
      4. Nearest unclaimed text/voice in the SAME section, up to 3 after

    claimed: set of message indices already used as descriptions. Updated
             in-place so subsequent calls skip those messages.
    """
    if claimed is None:
        claimed = set()

    m = messages[photo_idx]
    photo_cat = m.get('category', 'damaged')
    photo_num = m.get('photo_num')

    # 1. Inline caption sent with the photo (photo's own content — never shared)
    if m.get('content'):
        return m['content']

    def _is_available_text(idx):
        if idx in claimed:
            return False
        msg = messages[idx]
        content = (msg.get('content') or '').strip()
        return (bool(content)
                and not msg.get('media_path')
                and msg.get('category') == photo_cat)

    # 2. Explicit numeric reference within the same section (unclaimed only)
    if photo_num:
        search_range = list(range(max(0, photo_idx - 5), photo_idx)) + \
                       list(range(photo_idx + 1, min(len(messages), photo_idx + 6)))
        patterns = (f'photo {photo_num}', f'pic {photo_num}',
                    f'image {photo_num}', f'{photo_num}.')
        for k in search_range:
            if _is_available_text(k):
                txt = messages[k]['content'].lower()
                if any(p in txt for p in patterns):
                    claimed.add(k)
                    return messages[k]['content'].strip()

    # 3. Nearest unclaimed same-section text/voice BEFORE the photo
    for k in range(photo_idx - 1, max(photo_idx - 4, -1), -1):
        if _is_available_text(k):
            claimed.add(k)
            return messages[k]['content'].strip()

    # 4. Nearest unclaimed same-section text/voice AFTER the photo
    for k in range(photo_idx + 1, min(photo_idx + 4, len(messages))):
        if _is_available_text(k):
            claimed.add(k)
            return messages[k]['content'].strip()

    return ''


def _group_messages_by_category(messages: list) -> str:
    """Present messages to Claude grouped by section for clear context."""
    buckets = {
        'bridge_details':  [],
        'damaged':         [],
        'general':         [],
        'recommendations': [],
    }
    for m in messages:
        cat = m.get('category', '')
        text = (m.get('content') or '').strip()
        if text and cat in buckets:
            buckets[cat].append(text)

    parts = []
    if buckets['bridge_details']:
        parts.append("BRIDGE DETAILS (use for Section A fields):\n" +
                     '\n'.join(buckets['bridge_details']))
    if buckets['damaged']:
        parts.append("DAMAGE OBSERVATIONS (use for Section B defect fields):\n" +
                     '\n'.join(buckets['damaged']))
    if buckets['recommendations']:
        parts.append("RECOMMENDATIONS (use for Section C fields):\n" +
                     '\n'.join(buckets['recommendations']))
    if buckets['general']:
        parts.append("GENERAL SITE NOTES:\n" + '\n'.join(buckets['general']))
    return '\n\n'.join(parts)


def parse_inspection(session: dict) -> dict:
    """Send session messages to Claude and return structured JSON."""
    messages = session.get('messages', [])

    # Collect photo paths and their best available description
    photo_paths              = []
    photo_captions           = []   # raw description (used for circle detection)
    photo_descriptions       = []   # same, passed to Claude for title generation
    photo_categories_from_db = []   # set by menu state at collection time

    claimed_text_indices = set()   # track which text msgs have been used as photo captions

    for j, m in enumerate(messages):
        if m.get('media_path'):
            desc = _find_photo_description(messages, j, claimed_text_indices)
            photo_paths.append(m['media_path'])
            photo_captions.append(desc)
            photo_descriptions.append(desc)
            # Category is set by the menu state at collection time — no AI classification needed
            photo_categories_from_db.append(m.get('category', 'damaged'))

    grouped_notes = _group_messages_by_category(messages)

    # Assign figure numbers only to damage photos (these are the numbers used in Appendix B)
    fig_counter = 0
    photo_info  = []
    for i, (d, c) in enumerate(zip(photo_descriptions, photo_categories_from_db)):
        entry = {"description": d, "category": c}
        if c in ('damage', 'damaged'):
            fig_counter += 1
            entry["figure_no"] = fig_counter
            entry["reference"]  = f"(Photo No.-{fig_counter})"
        else:
            entry["figure_no"] = None
            entry["reference"]  = None   # general photos are not referenced in observations
        photo_info.append(entry)

    print(f"PARSE: {len(messages)} messages, photos={len(photo_paths)}")
    print(f"GROUPED NOTES:\n{grouped_notes}")
    print(f"PHOTO INFO: {photo_info}")

    user_content = (
        f"Schema:\n{json.dumps(SCHEMA, indent=2)}\n\n"
        f"Field notes (grouped by section):\n{grouped_notes}\n\n"
        f"Photo information (use 'reference' value when citing each photo in observation fields):\n"
        f"{json.dumps(photo_info, indent=2)}\n\n"
        f"Photo file paths (in sequence order):\n{json.dumps(photo_paths)}\n\n"
        "For photo_titles: generate a short title (max 10 words) per photo from its description. "
        "photo_titles must have exactly the same count as photo file paths.\n\n"
        "For photo_categories: use the category values from Photo information — do NOT reclassify. "
        "photo_categories must have exactly the same count as photo file paths.\n\n"
        "For observation fields (ss_*, sub_*, found_*, bearing_*, approach_*, expansion_joint, "
        "wearing_coat, vegetation): when a defect is observed, append the 'reference' value from "
        "matching damage photos, e.g. \"Observed (Photo No.-1), (Photo No.-3)\". "
        "Only reference damage photos (those with a non-null reference). "
        "Do NOT add references to Absent / Not Visible / NIL / NA fields."
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
    result['_messages']      = messages   # passed to build_docx for BLOB restoration

    # Ensure photo_titles has the right count; fall back to first 10 words of description
    titles = result.get('photo_titles') or []
    if len(titles) != len(photo_paths):
        titles = [' '.join(d.split()[:10]) if d else '' for d in photo_descriptions]
    result['photo_titles'] = titles

    # Categories come from DB (menu state) — use as authoritative source
    cats = photo_categories_from_db if len(photo_categories_from_db) == len(photo_paths) \
           else (result.get('photo_categories') or ['damaged'] * len(photo_paths))

    result['photo_categories'] = cats

    # Apply defect circle marking ONLY to damaged photos — run in parallel
    import concurrent.futures

    def _mark_one(args):
        path, desc = args
        try:
            with open(path, 'rb') as f:
                original = f.read()
            marked = mark_defect(original, desc)
            if marked != original:
                with open(path, 'wb') as f:
                    f.write(marked)
                print(f"CIRCLE MARKED: {path}")
        except Exception as e:
            print(f"CIRCLE MARK FAILED for {path}: {e}")

    mark_jobs = [
        (path, desc)
        for path, desc, cat in zip(photo_paths, photo_descriptions, cats)
        if cat in ('damage', 'damaged') and desc and os.path.exists(path)
    ]
    if mark_jobs:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            list(pool.map(_mark_one, mark_jobs))

    print(f"PHOTOS injected: {photo_paths}")
    print(f"TITLES: {result['photo_titles']}")
    print(f"CATEGORIES: {result['photo_categories']}")
    return result
