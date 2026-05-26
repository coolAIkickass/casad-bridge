# ai_parse.py — Claude API: field notes → structured JSON
import json, os, time
import anthropic
from mark_image import get_defect_coords

client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

# Claude Haiku rate limit: 5 req/min → 1 req per 13s (with small buffer)
_COORD_DELAY_SEC = 13

def _safe_json_parse(raw: str) -> dict:
    """Parse JSON from Claude, repairing common truncation issues.

    Claude occasionally truncates output even with generous max_tokens when the
    response is very large. This function tries:
      1. Direct json.loads (normal case)
      2. json_repair library if installed
      3. Close any open brackets/strings caused by truncation
    """
    # 1 — happy path
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2 — json_repair library (pip install json-repair)
    try:
        import json_repair
        obj = json_repair.repair_json(raw, return_objects=True)
        if isinstance(obj, dict):
            print("JSON repaired via json_repair library")
            return obj
    except ImportError:
        pass
    except Exception as e:
        print(f"json_repair failed: {e}")

    # 3 — manual repair: close unclosed strings, brackets, braces
    try:
        stack = []
        in_string = False
        escape = False
        out = []
        for ch in raw:
            out.append(ch)
            if escape:
                escape = False
                continue
            if ch == '\\' and in_string:
                escape = True
                continue
            if ch == '"':
                if in_string:
                    in_string = False
                else:
                    in_string = True
                continue
            if not in_string:
                if ch == '{':
                    stack.append('}')
                elif ch == '[':
                    stack.append(']')
                elif ch in ('}', ']'):
                    if stack and stack[-1] == ch:
                        stack.pop()

        # Close any unterminated string
        if in_string:
            out.append('"')
        # Close any unclosed structures (innermost first)
        out.extend(reversed(stack))

        repaired = ''.join(out)
        result = json.loads(repaired)
        print(f"JSON repaired manually: closed {len(stack)} open bracket(s)")
        return result
    except Exception as e:
        raise ValueError(
            f"Claude JSON could not be parsed or repaired: {e}\n"
            f"First 300 chars: {raw[:300]}"
        )


def _detect_defect_coords(photo_paths, photo_descriptions, cats):
    """Detect defect bounding coords for damage photos — sequential with rate-limit delay.

    Processes one photo every 13 seconds to stay within the 5 req/min Claude Haiku
    limit. For 80 damage photos this takes ~17 min, which is acceptable.
    Returns: dict {path: (x, y) or None}
    """
    photo_coords = {path: None for path in photo_paths}
    jobs = [
        (path, desc)
        for path, desc, cat in zip(photo_paths, photo_descriptions, cats)
        if cat in ('damage', 'damaged') and desc and os.path.exists(path)
    ]
    print(f"DEFECT COORDS: {len(jobs)} damage photos to process (~{len(jobs)*_COORD_DELAY_SEC//60} min)", flush=True)
    for i, (path, desc) in enumerate(jobs):
        if i > 0:
            time.sleep(_COORD_DELAY_SEC)
        try:
            with open(path, 'rb') as f:
                img_bytes = f.read()
            coords = get_defect_coords(img_bytes, desc)
            photo_coords[path] = coords
            print(f"DEFECT COORDS [{i+1}/{len(jobs)}]: {coords} for: {desc[:60]}", flush=True)
        except Exception as e:
            print(f"DEFECT COORDS FAILED [{i+1}/{len(jobs)}]: {e}", flush=True)
    return photo_coords

SYSTEM_PROMPT = '''
You are a structural engineering report assistant for CASAD Consultants with deep knowledge of bridge components.
Convert informal field notes into a structured JSON object matching the CASAD bridge inspection report format.
Notes may be in mixed Hindi/English or fragmented.
Output ONLY valid JSON — no markdown, no explanation, no preamble.

ALWAYS APPLY — Unit and operator normalization (applies to ALL fields, ALL values, no exceptions):
- "meter / meters / metre / metres" → "m"  (e.g. "6.5 meter" → "6.5 m")
- "kilometer / kilometres" → "km"
- "plus" → "+"  (e.g. "6.5 m plus 2.5 m" → "6.5 m + 2.5 m")
- "minus" → "−"
- "into" / "times" / "multiplied by" → "×"
Apply this normalization throughout every extracted value — these are formatting conventions only.

CRITICAL — Exact value preservation (never override):
- Copy ALL values EXACTLY as the inspector stated (after applying unit/operator normalization above). Do NOT paraphrase, abbreviate, rephrase, or translate technical terms.
- For span lengths and bridge lengths, preserve EXACTLY what the inspector stated — nothing more, nothing less.
  If the inspector stated a full breakdown: "92 + 6 × 25 + 21 + 13 + 63 = 339.53 m" → write that exactly.
  If the inspector stated only a total: "657 m" → write only "657 m" — NEVER derive or expand a math breakdown.
  WRONG: deriving "31.5 + 38.5 + 3×31.5 + … = 657 m" when inspector said only "657 m"
  For formatting: use × not x, preserve spaces around operators.
- For GPS coordinates, preserve FULL decimal precision as stated (e.g. "23.007695" not "23").
- For angles, preserve the exact symbol/code stated (e.g. "Q" means Q — do NOT convert to "Skew").
- For no_of_spans, list each side separately: "Anupam Road Side: 10 Nos.\nGomtipur Road Side: 10 Nos.\nRailway Portion: 4 Nos."
- Leave a field as "" (empty string) if the inspector did NOT mention it — never invent or infer values.
  This rule applies to EVERY field without exception. Do not fill any field unless the inspector explicitly stated it.

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

CRITICAL — Field assignment rules (common confusion points):
- bearing_type_detail: ONLY the type of bearing (e.g. "Elastomeric Bearing", "POT-PTFE Bearing", "Metallic Bearing"). NEVER put expansion joint info here.
- expansion_joint: ONLY the expansion joint type (e.g. "Strip Seal", "Compression Seal"). NEVER put bearing info here.
- hydraulic_parameters: write "Not Applicable" for Railway Over Bridges (ROB) or bridges without waterway. Write actual HFL/discharge data if given.
- subsoil_particulars: write "As per approved GAD" if inspector says so, or actual soil description.

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
- "" (empty string, NOT "Absent") for ALL defect fields — if inspector did NOT mention a defect for a pier/span, leave it EMPTY. Do NOT write "Absent". The report system will leave those cells blank.
- "" (empty string) for all other missing fields
- NEVER use "Absent", "NIL", "NA", "-" as fill-in values for fields the inspector did not mention.
- EXCEPTION: if inspector explicitly says "no cracks" / "no defects observed" / "absent", then write "Absent" for that specific field only.

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
  - If a defect is observed WITH a photo, append the relevant photo numbers: "Observed (Photo No.-1), (Photo No.-3)"
  - If a defect is reported in OBSERVATIONS (NO PHOTO) section, write "Observed" WITHOUT any photo reference
  - Match photo numbers using the Photo information list provided — reference photos whose description mentions that specific defect and component
  - Only add photo references when the value is "Observed" or a specific description — NOT for "Absent", "Not Visible", "NA", "NIL", "-"
  - Format exactly: "Observed (Photo No.-X)" or "Observed (Photo No.-X), (Photo No.-Y)"
  - If both a photo AND an observation-only note exist for the same defect, combine: "Observed (Photo No.-1)" (do not duplicate the word Observed)

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
    "latitude":             "",    # FULL decimal precision (e.g. "23.007695")
    "longitude":            "",    # FULL decimal precision
    "circle":               "",
    "division":             "",
    "sub_division":         "",
    "no_of_spans":          "",    # list each side: "Anupam: 10 Nos.\nGomtipur: 10 Nos.\nRailway: 4 Nos."
    "cc_of_piers":          "",    # C/C spacing per side: "25 m (Anupam Cinema side)\n12.5 m (Gomtipur Road Side)"
    "width_of_piers":       "",    # pier width per side: "1.25 m (Anupam Cinema side)\n1.2 m (Gomtipur Road Side)"
    "span_length":          "",    # legacy combined field (C/C + pier widths) — keep for backward compat
    "span_arrangement":     "",    # EXACT mathematical breakdown: "92+6×25+21+13+63=339.53m (Anupam)..."
    "total_length":         "",    # same as span_arrangement — full expression
    "bridge_type":          "",    # structural type (PSC Girder / RCC Slab / Steel Truss)
    "bridge_level_type":    "",    # high level / submersible / ROB — from "type of bridge whether high level..."
    "type_of_bridge":       "",    # same as bridge_level_type
    "superstructure_type":  "",    # PSC Girder / RCC Slab / Steel Truss with location suffixes
    "substructure_type":    "",
    "foundation_type":      "",
    "bearing_type_detail":  "",
    "approach_length":      "",
    "railing_type":         "",
    "river_training":       "",
    "repair_work":          "",
    "carriage_width":       "",    # ONLY fill if inspector explicitly stated
    "year_of_construction": "",
    "river_perennial":      "",
    "angle_of_crossing":    "",    # copy EXACTLY (e.g. "Q" stays "Q", not "Skew")
    "hydraulic_parameters":  "",    # "Not Applicable" for ROBs / actual value if given
    "subsoil_particulars":   "",    # "As per approved GAD" / actual description
    "deck_level":           "",    # e.g. "107.575 m (Railway Portion)"
    "pier_length":          "",    # e.g. "2.5 m (Anupam Cinema side)\n3.0 m (Railway Portion)"
    "date_of_completion":   "",    # dd/mm/yyyy
    "surface_utilities":    "",    # electric lines, telephone cables etc.
    "performance":          "",    # Good / Fair / Poor
    "prestressing_details": "",    # "As per approved Design" if mentioned

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


EXCEL_SCHEMA = {
    **SCHEMA,  # include all standard fields
    # Excel-specific additions
    "client_name":       "",
    "project_name":      "",
    "project_number":    "",
    "bridge_title":      "",  # full official bridge name

    # ── Appendix-A additional fields ─────────────────────────────────────────
    "bm_gts_level":              "",  # BM / GTS benchmark level
    "pier_width_detail":         "",  # width of pier (e.g. "1.2 m and 0.6 m")
    "pier_cap_width":            "",  # width of pier cap
    "abutment_width":            "",  # width of abutment
    "abutment_cap_width":        "",  # width of abutment cap
    "returns_length":            "",  # length of returns/wing walls
    "date_of_construction_start": "", # date construction started (dd/mm/yyyy)
    "design_agency":             "",  # name of design agency
    "construction_agency":       "",  # name of construction agency

    # ── Appendix-A Section 3 — Hydraulic Parameters (sub-rows 21–31) ─────────
    "hydraulic_catchment":      "",   # catchment area
    "hydraulic_discharge":      "",   # designed discharge
    "hydraulic_hfl":            "",   # designed HFL
    "hydraulic_ofl":            "",   # ordinary flood level
    "hydraulic_clearance":      "",   # vertical clearance
    "hydraulic_lwl":            "",   # low water level
    "hydraulic_depth":          "",   # depth of flow during HFL
    "hydraulic_velocity":       "",   # designed velocity of flood
    "hydraulic_channel_width":  "",   # width of channel at max HFL
    "hydraulic_spread":         "",   # spread of water at max HFL
    "hydraulic_bed_level":      "",   # average bed level

    # ── Appendix-A Section 4 — Sub Soil Particulars (sub-rows 34–39) ─────────
    "subsoil_type":             "",   # type of soil
    "subsoil_friction":         "",   # angle of internal friction
    "subsoil_cohesion":         "",   # cohesion "C"
    "subsoil_silt_factor":      "",   # silt factor
    "subsoil_bearing_capacity": "",   # safe bearing capacity
    "subsoil_foundation_level": "",   # actual foundation level from pile cap bottom

    # ── Appendix-A Section 5 — Design & Structural Data (gaps) ──────────────
    "loading_standard":         "",   # loading standards / seismic coefficient
    "design_scour_level":       "",   # designed maximum scour level
    "design_foundation_level":  "",   # designed foundation level from pile cap bottom
    "substructure_material":    "",   # (i) masonry / mass concrete / RCC
    "articulation_details":     "",   # (iii) details of articulation
    "total_load_foundation":    "",   # total load at foundation level
    "total_horizontal_force":   "",   # total horizontal force at scour level
    "protection_works":         "",   # details of protection works
    "model_studies":            "",   # whether model studies conducted
    "special_design_features":  "",   # details of special design features
    "settlement_report":        "",   # report of settlement / scour during construction

    # ── Appendix-A Material Consumed (row 73 header + rows 74–77) ────────────
    "material_consumed":        "",   # section-level: "Data Not Available" or similar
    "material_cement":          "",   # quantity of cement consumed
    "material_reinforcement":   "",   # quantity of reinforcing steel
    "material_structural_steel":"",   # quantity of structural steel
    "material_hts_steel":       "",   # quantity of HTS steel

    # ── Appendix-A Other Data (row 84 = LS sketch, row 83 = design drawings) ─
    "ls_sketch":                "",   # diagrammatic sketch / LS of bridge showing span arrangements, bed profile and RLs
    "design_drawings":          "",   # design details and drawings reference
    "special_features":         "",   # special constructional / design features
    "total_cost":               "",   # total cost of bridge
    "cost_per_sqm_carriageway": "",   # rate per sq m carriageway
    "cost_per_sqm_elevation":   "",   # rate per sq m elevation
    "cost_per_m_length":        "",   # rate per m length

    # ── Appendix-B Section 10 — Steel sub-rows (71–78) ───────────────────────
    "steel_paint":              "",   # 10.2.1 condition of paint
    "steel_corrosion":          "",   # 10.2.2 corrosion
    "steel_vibration":          "",   # 10.2.3 perceptible vibrations
    "steel_alignment":          "",   # 10.2.4 alignment of members
    "steel_connections":        "",   # 10.2.5 condition of connections
    "steel_camber_deflection":  "",   # 10.2.6 camber and deflection
    "steel_buckling":           "",   # 10.2.7 buckling
    "steel_cleanliness":        "",   # 10.2.8 cleanliness of members/joints

    # ── Appendix-B Section 10 — Masonry sub-rows (80–85) ────────────────────
    "masonry_joints":           "",   # 10.3.1 condition of joints/mortar/pointing
    "masonry_profile":          "",   # 10.3.2 profile / rise of arch
    "masonry_cracks":           "",   # 10.3.3 cracks
    "masonry_drainage":         "",   # 10.3.4 drainage of spandrel fillings
    "masonry_vegetation":       "",   # 10.3.5 growth of vegetation
    "masonry_other":            "",   # 10.3.6 any other observations

    # ── Appendix-B Section 10 — Timber sub-rows (87–90) ─────────────────────
    "timber_paint":             "",   # 10.4.1 condition of paint
    "timber_decay":             "",   # 10.4.2 decay / wear / structural defects
    "timber_joints":            "",   # 10.4.3 condition of joints / splices
    "timber_sag":               "",   # 10.4.4 excessive sag

    # ── Appendix-B Section 4 — Approaches (individual rows) ──────────────────
    "approach_side_slopes": "",  # 4.2 side slopes condition
    "approach_slab":        "",  # 4.4 approach slab condition
    "approach_geometrics":  "",  # 4.5 approach geometrics

    # ── Appendix-B Section 5 — Protective Works ──────────────────────────────
    "prot_type":            "",  # 5.1 type of protective works
    "prot_damage_layout":   "",  # 5.2 damage to layout / cross-section
    "prot_slope_pitching":  "",  # 5.3 slope pitching / apron / toe walls
    "prot_floor_protection":"",  # 5.4 floor protection works
    "prot_scour_extent":    "",  # 5.5 extent of scour
    "prot_reserve_stone":   "",  # 5.6 reserve stone material
    "prot_other":           "",  # 5.7 any other observation

    # ── Appendix-B Section 6 — Waterway ──────────────────────────────────────
    "waterway_obs":         "",  # section-level summary (e.g. "Not Applicable")
    "waterway_obstruction": "",  # 6.1 obstructions/undergrowth
    "waterway_scour":       "",  # 6.2 scour
    "waterway_flow":        "",  # 6.3 change in flow pattern
    "waterway_flood_level": "",  # 6.4 max flood level observed
    "waterway_afflux":      "",  # 6.5 afflux
    "waterway_adequacy":    "",  # 6.6 adequacy of waterway
    "waterway_other":       "",  # 6.7 any other observation

    # ── Appendix-B Section 7 — Foundations ───────────────────────────────────
    "foundations_obs":       "",  # section-level summary (e.g. "Not Visible")
    "foundations_settlement":"",  # 7.1 settlement
    "foundations_cracking":  "",  # 7.2 cracking/disintegration
    "foundations_floating":  "",  # 7.3 floating bodies damage
    "foundations_subway":    "",  # 7.4 subway seepage
    "foundations_other":     "",  # 7.5 any other observation

    # ── Appendix-B Section 8 — Substructure ──────────────────────────────────
    "sub_section_obs":      "",  # 8 header (e.g. "Refer Table 1 and 2" if user says so)
    "sub_drainage_backfill":"",  # 8.1 backfill drainage / weep holes
    "sub_cracking_obs":     "",  # 8.2 cracking / disintegration
    "sub_subway_obs":       "",  # 8.3 subway retaining walls
    "sub_other_obs":        "",  # 8.4 any other observation

    # ── Appendix-B Section 9 — Bearings ──────────────────────────────────────
    "bear_metallic_type":       "",  # 9.1 metallic bearings (type / "Not Applicable")
    "bear_metallic_condition":  "",  # 9.1.1 condition
    "bear_metallic_functioning":"",  # 9.1.2 functioning
    "bear_metallic_greasing":   "",  # 9.1.3 greasing/oil bath
    "bear_pedestal_cracks":     "",  # 9.1.4 cracks in pedestal/pier cap
    "bear_metallic_anchor":     "",  # 9.1.5 anchor bolts
    "bear_metallic_other":      "",  # 9.1.6 any other
    "bear_elastomeric_type":    "",  # 9.2 elastomeric bearings (type / "Not Applicable")
    "bear_pad_condition":       "",  # 9.2.1 pad condition
    "bear_cleanliness":         "",  # 9.2.2 cleanliness
    "bear_elastomeric_other":   "",  # 9.2.3 any other

    # ── Appendix-B Section 10 — Superstructure ───────────────────────────────
    "super_section_obs":     "",  # 10 header (e.g. "Refer Table 3 and 4" if user says so)
    "super_spalling_obs":    "",  # 10.1.1 spalling/honeycombing
    "super_cracking_obs":    "",  # 10.1.2 cracking
    "super_corrosion_obs":   "",  # 10.1.3 corrosion of reinforcement
    "super_vehicle_damage":  "",  # 10.1.4 damage due to vehicles
    "super_articulation":    "",  # 10.1.5 articulation condition
    "super_vibration":       "",  # 10.1.6 perceptible vibration
    "super_deflection":      "",  # 10.1.7 excessive deflection / loss of camber
    "super_anchorage_cracks":"",  # 10.1.8 cracks in end anchorage zone
    "super_hinge_deflection":"",  # 10.1.9 deflection at central hinge
    "steel_obs":             "",  # 10.2 steel members summary
    "masonry_obs":           "",  # 10.3 masonry arches summary
    "timber_obs":            "",  # 10.4 timber members summary

    # ── Appendix-B Section 11 — Expansion Joints ─────────────────────────────
    "exp_jt_functioning":   "",  # 11.1 functioning / gap condition
    "exp_jt_sealing":       "",  # 11.2 sealing material condition
    "exp_jt_fixing":        "",  # 11.3 condition at fixing points
    "exp_jt_sliding_plate": "",  # 11.4 top sliding plate corrosion
    "exp_jt_locking":       "",  # 11.5 locking of joints
    "exp_jt_debris":        "",  # 11.6 debris in open joints
    "exp_jt_rattling":      "",  # 11.7 rattling
    "exp_jt_other":         "",  # 11.8 any other observation

    # ── Appendix-B Section 12 — Wearing Coat ─────────────────────────────────
    "wear_coat_type":       "",  # 12 type (e.g. "Bituminous")
    "wear_coat_surface":    "",  # 12.1 surface condition
    "wear_coat_evidence":   "",  # 12.2 evidence of wear

    # ── Appendix-B Section 13 — Drainage Spouts ──────────────────────────────
    "drain_type":           "",  # 13 overall condition / type
    "drain_clogging":       "",  # 13.1 clogging/deterioration
    "drain_projection":     "",  # 13.2 projection on underside
    "drain_adequacy":       "",  # 13.3 adequacy
    "drain_subway":         "",  # 13.4 subway pumping
    "drain_other":          "",  # 13.5 any other

    # ── Appendix-B Section 14 — Handrail ─────────────────────────────────────
    "handrail_condition":   "",  # 14.1 general condition
    "handrail_collision":   "",  # 14.2 collision damage
    "handrail_alignment":   "",  # 14.3 alignment

    # ── Appendix-B Section 15 — Footpath ─────────────────────────────────────
    "footpath_condition":   "",  # 15.1 general condition
    "footpath_missing_slab":"",  # 15.2 missing footpath slab
    "footpath_other":       "",  # 15.3 any other observation

    # ── Appendix-B Section 16 — Utilities ────────────────────────────────────
    "utilities_obs":        "",  # 16 section-level summary
    "util_water_leakage":   "",  # 16.1 water/sewage pipe leakage
    "util_cable_damage":    "",  # 16.2 telephone/electric cable damage
    "util_lighting":        "",  # 16.3 lighting condition
    "util_other_damage":    "",  # 16.4 other utility damage

    # ── Appendix-B Section 17-19 & Overall ───────────────────────────────────
    "bridge_num_condition":   "",  # 17.1 bridge number / painting condition
    "aesthetics_intrusion":   "",  # 18.1 visual intrusion
    "maintenance_history":    "",  # 19 maintenance done since last inspection
    "overall_condition_visual":"", # overall visual condition (unlabelled row)

    # ── Defect table pier/span lists ──────────────────────────────────────────
    "sub_piers_side1":   [],  # pier ID strings for substructure side 1
    "sub_side1_label":   "",
    "sub_piers_side2":   [],
    "sub_side2_label":   "",
    "super_spans_side1": [],  # span ID strings for superstructure side 1
    "super_side1_label": "",
    "super_spans_side2": [],
    "super_side2_label": "",
    "defect_sub1":   {},  # {pier_id: {defect_key: observation}}
    "defect_sub2":   {},
    "defect_super1": {},
    "defect_super2": {},
}

EXCEL_EXTRA_PROMPT = '''
EXCEL FORMAT ADDITIONAL INSTRUCTIONS:

Extract pier and span IDs from the bridge details and inspection messages:
- sub_piers_side1/sub_piers_side2: list of pier/abutment IDs for each side (e.g. ["A1","RP1","P2","P3"])
- super_spans_side1/super_spans_side2: list of span IDs (e.g. ["A1-RP1","RP1-P2","P2-P3"])
- sub_side1_label/sub_side2_label: descriptive label for each side (e.g. "Railway + Anupam Cinema Side")

For defect matrices (defect_sub1, defect_sub2, defect_super1, defect_super2):
- Build a dict: {pier_or_span_id: {defect_key: observation_string}}
- Defect keys: cracks, leaching, honeycombing, exposed_rebar, leakage, spalling, rust_marks, shuttering, delamination, vegetation, any_other
- For any pier/span not mentioned for a specific defect, use "" (empty string — do NOT write "Absent")
- If a defect IS observed at a pier/span, write the specific description
- If only one side is mentioned, leave side2 empty list

NUMBER-WORD TO NUMERAL NORMALISATION:
When the inspector uses word-form numbers in "Table" or "Figure/Picture/Photo" references,
convert them to digits in the stored value. Apply this to every occurrence in the value.
Examples:
  "refer table one and two"        → "Refer Table 1 and 2"
  "refer table three and four"     → "Refer Table 3 and 4"
  "refer picture one"              → "Refer Picture 1"
  "figure one two three"           → "Figure 1, 2, 3"
  "photo number two"               → "Photo No.-2"
Mapping: one→1, two→2, three→3, four→4, five→5, six→6, seven→7, eight→8, nine→9, ten→10
This rule applies only inside table/figure/picture/photo reference phrases, not to other values.

Extract from bridge details:
- client_name: the government/municipal body that commissioned the inspection
- project_name: the broader project name (e.g. "Bridge Inspection Work Ahmedabad City")
- project_number: project reference number if mentioned
- bridge_title: official full bridge name

BRIDGE DETAILS EXTRACTION RULES (critical for Excel Appendix-A accuracy):
- no_of_spans: contains ONLY the span count/arrangement description. Stop extracting into this field
  the moment you encounter a phrase matching the Row 16 label ("length of bridge between decking" /
  "total length" / "length of bridge"). That phrase and its answer go to total_length only.
  Approach lengths (e.g. "75 m approach + 57.5 m approach") DO belong inside no_of_spans — they
  describe span layout.
  Format using mathematical notation — convert English words to symbols (per the normalization rule):
    "11 span of 31.5 m plus 38.5 m one span plus three spans of 31.5 m plus four spans of 28 m
     plus two spans of 32.5 m plus 75 m approach plus 57.5 m another approach"
    → "11 Spans: 1 × 31.5 m + 1 × 38.5 m + 3 × 31.5 m + 4 × 28 m + 2 × 32.5 m\n75 m approach + 57.5 m approach"
- total_length / span_arrangement: preserve the EXACT mathematical expression if given; if only a
  total is given (e.g. "657 m"), write only that — NEVER derive an expansion.
- superstructure_type: list each part with span range, e.g.:
  "PSC Girder and Deck Slab (RP1 to P8 — Anupam Cinema Side)\nRCC Solid Slab (P5 to BA1 — Gomtipur Side)\nSteel Truss (Railway Portion)"
- bridge_level_type / type_of_bridge: "High Level" / "ROB" / "Submersible" — NOT the structural type.
- angle_of_crossing: copy EXACTLY as stated — if inspector says "Q", output "Q" (not "Skew").
- latitude / longitude: preserve FULL decimal precision exactly as stated.
- cc_of_piers: ONLY the C/C (centre-to-centre) spacing per side, e.g.:
  "25 m (Anupam Cinema side)\n12.5 m (Gomtipur Road Side)"
- width_of_piers: ONLY populate when the inspector mentions pier width in the context of NUMBER OF
  SPANS or span layout (e.g. "C/C 25 m, pier width 1.25 m"). If pier width is mentioned only in
  the substructure section (while describing pier dimensions/type) → put it in pier_width_detail
  only; leave width_of_piers empty.
- span_length: legacy combined field — if the inspector gives C/C and pier width together (e.g. "C/C 25m, pier width 1.25m"), populate span_length AND also extract cc_of_piers and width_of_piers separately if distinguishable.
- hydraulic_parameters: if inspector says "hydraulic parameters not applicable" or similar, output "Not Applicable". For ROBs, this is always "Not Applicable".
- subsoil_particulars: if inspector says "as per approved GAD" or "as per approved design", output exactly that phrase. Leave empty if not mentioned.
- bearing_type_detail: list each bearing type with its location. e.g. "Elastomeric Bearing (Anupam Cinema + Gomtipur Side)\nPOT-PTFE Bearing (Railway Portion)". NEVER put expansion joint text here.
- date_of_completion: dd/mm/yyyy format, e.g. "09/08/2020"
- date_of_construction_start: dd/mm/yyyy format — from "date of starting construction"
- construction_agency: name of the contractor/construction agency
- design_agency: name of the design agency
- pier_width_detail: width of piers (e.g. "1.2 m and 0.6 m") — IMPORTANT: apply unit normalization throughout the ENTIRE value, including every occurrence of "meter" within the string. e.g. "6 meter and 3 meter" → "6 m and 3 m" (BOTH occurrences normalized, not just the first).
- pier_length: same normalization rule — "6 meter and 3 meter" → "6 m and 3 m"
- pier_cap_width: same normalization rule — every "meter" in the value → "m"
- abutment_width: width of abutment
- abutment_cap_width: width of abutment cap
- returns_length: length of return walls / wing walls (e.g. "100 m Shamal side + 70 m Jivraj side = 170 m")
- substructure_type: the OVERALL structural form of the substructure — e.g. "Straight Pier", "T-Pier", "Portal Frame", "Hammerhead", "Wall Pier". This is the SHAPE/FORM description, NOT the material. Leave empty if the inspector did not explicitly state the overall structural form. NEVER combine a material name ("RCC") with a row-label word ("Straight") to construct a value like "RCC Straight" — if the inspector only said "RCC" and the next row title begins with "Straight", that "Straight" belongs to the next row label, not to substructure_type.
- substructure_material: the material selected from the row "(i) Masonry, Mass Concrete, RCC" — just the chosen material, e.g. "RCC", "Masonry", "Mass Concrete". CRITICAL: when the inspector says only "RCC" (or only "Masonry") in the context of the substructure material option-list, fill ONLY substructure_material; do NOT also fill substructure_type with the same value.
- material_consumed: the section-level answer for the "Material Consumed" header row — typically "Data Not Available" or "As per approved records". Fill when the inspector gives a general response for the whole section rather than listing individual quantities. Do NOT repeat this value in material_cement / material_reinforcement etc.
- ls_sketch: for "Diagrammatic sketch / give LS of bridge showing span arrangements, bed profile and RLs of salient features and soil profiles on actual bore data". When inspector says "data not applicable" / "not applicable" for this → ls_sketch = "Data Not Applicable". Voice cue: inspector reads "diagrammatic sketch give ls of bridge showing span arrangement bed profile rls...".

APPENDIX-B FIELD EXTRACTION (Inspection Observations):
Map each inspector observation to the most specific matching field. Write the value EXACTLY as given.
- "Not Applicable", "Absent", "Not Visible", "Good", "Damaged" etc. are all valid values — write them as-is.
- Each field maps to exactly one row in Appendix-B. Do NOT combine multiple rows into one field.
- SOURCE RULE: Appendix-B observation fields must come ONLY from the inspector's typed/spoken field notes. NEVER use photo captions or photo descriptions to fill these fields. If a photo shows a crack but the inspector said "Not Applicable" for that row — write "Not Applicable".
- If the inspector says "Not Applicable" or "Refer Table X" for an entire section header, put that value ONLY in the section-level field — NOT in any sub-row fields. Each sub-row field must have its own explicit observation to be filled.
- For substructure section (sub_section_obs) and superstructure section (super_section_obs): if the inspector explicitly says "refer table 1 and 2" or "refer table 3 and 4", copy that text exactly into the field. Do not add these references on your own. Do NOT repeat or cascade that value into sub-rows (sub_spalling_obs, super_spalling_obs, super_cracking_obs, etc.).
- Section 10 HEADER (super_section_obs): when the inspector begins section 10 by stating the type of superstructure (e.g. "superstructure PSC box girder"), record that type in super_section_obs — NOT in superstructure_type. superstructure_type is for Appendix-A only; in Appendix-B section 10, the type stated at the header level IS the super_section_obs value.
- Section 10 row label contains defect keywords ("spalling", "disintegration", "honeycombing"): when the inspector recites the 10.1.1 label and then answers "Refer Table 3 and 4", those defect words are part of the LABEL, not the answer. Fill only super_section_obs = "Refer Table 3 and 4" when the inspector collapses the entire section into one global answer; leave all 10.1.x sub-row fields empty in that case.
  Example A — inspector gives individual row answers (most common):
  "superstructure psc box girder report spalling disintegration honeycombing refer table three and four report cracking refer table 3 and 4 report corrosion of reinforcement refer table 3 and 4"
  → super_section_obs = "PSC Box Girder"  (type stated at section header)
  → super_spalling_obs = "Refer Table 3 and 4"  (row 10.1.1 answer)
  → super_cracking_obs = "Refer Table 3 and 4"  (row 10.1.2 answer)
  → super_corrosion_obs = "Refer Table 3 and 4"  (row 10.1.3 answer)
  Example B — inspector collapses entire section into one global answer:
  "superstructure psc box girder refer table three and four"
  → super_section_obs = "PSC Box Girder — Refer Table 3 and 4"
  → super_spalling_obs = ""  (NOT separately filled)
  → super_cracking_obs = ""  (NOT separately filled)

Section mapping guide:
  approach_settlement → 4.1 (pavement surface condition of approaches)
    Voice pattern example: "state the condition of pavement surface crack rust vegetation"
    → inspector reads row label "State the condition of pavement surface..." then answers
    → approach_settlement = "Crack, rust, vegetation"  (extract the answer after the label)
  approach_side_slopes → 4.2 (side slopes)
  approach_erosion → 4.3 (erosion of embankment)
  approach_slab → 4.4 (approach slab)
  approach_geometrics → 4.5 (approach geometrics)
  approach_other → 4.6 (any other specific observations for approaches)
    IMPORTANT: if inspector says "not applicable" for approach_other, write "Not Applicable".
    Do NOT fill this field from photo captions or descriptions — only from inspector's spoken/typed notes.

  prot_type → 5.1, prot_damage_layout → 5.2, prot_slope_pitching → 5.3,
  prot_floor_protection → 5.4, prot_scour_extent → 5.5, prot_reserve_stone → 5.6, prot_other → 5.7
  SECTION 5 SPECIAL RULE: Section 5 (Protective Works) has no section-level observation field.
  If the inspector says "protective works not applicable" or "section 5 not applicable" without
  giving individual sub-row observations → fill ALL of prot_type, prot_damage_layout,
  prot_slope_pitching, prot_floor_protection, prot_scour_extent, prot_reserve_stone, prot_other
  with "Not Applicable". Do NOT put it only in prot_type.

  waterway_obs → section 6 header, waterway_obstruction → 6.1, waterway_scour → 6.2,
  waterway_flow → 6.3, waterway_flood_level → 6.4, waterway_afflux → 6.5,
  waterway_adequacy → 6.6, waterway_other → 6.7

  foundations_obs → section 7 header, foundations_settlement → 7.1, foundations_cracking → 7.2,
  foundations_floating → 7.3, foundations_subway → 7.4, foundations_other → 7.5

  sub_section_obs → section 8 header, sub_drainage_backfill → 8.1, sub_cracking_obs → 8.2,
  sub_subway_obs → 8.3, sub_other_obs → 8.4

  bear_metallic_type → 9.1, bear_metallic_condition → 9.1.1, bear_metallic_functioning → 9.1.2,
  bear_metallic_greasing → 9.1.3, bear_pedestal_cracks → 9.1.4, bear_metallic_anchor → 9.1.5,
  bear_metallic_other → 9.1.6, bear_elastomeric_type → 9.2, bear_pad_condition → 9.2.1,
  bear_cleanliness → 9.2.2, bear_elastomeric_other → 9.2.3

  super_section_obs → section 10 header, super_spalling_obs → 10.1.1, super_cracking_obs → 10.1.2,
  super_corrosion_obs → 10.1.3, super_vehicle_damage → 10.1.4, super_articulation → 10.1.5,
  super_vibration → 10.1.6, super_deflection → 10.1.7, super_anchorage_cracks → 10.1.8,
  super_hinge_deflection → 10.1.9, steel_obs → 10.2, masonry_obs → 10.3, timber_obs → 10.4

  exp_jt_functioning → 11.1, exp_jt_sealing → 11.2, exp_jt_fixing → 11.3,
  exp_jt_sliding_plate → 11.4, exp_jt_locking → 11.5, exp_jt_debris → 11.6,
  exp_jt_rattling → 11.7, exp_jt_other → 11.8

  wear_coat_type → 12, wear_coat_surface → 12.1, wear_coat_evidence → 12.2

  drain_type → 13, drain_clogging → 13.1, drain_projection → 13.2,
  drain_adequacy → 13.3, drain_subway → 13.4, drain_other → 13.5

  handrail_condition → 14.1, handrail_collision → 14.2, handrail_alignment → 14.3
  footpath_condition → 15.1, footpath_missing_slab → 15.2, footpath_other → 15.3
  utilities_obs → section 16 header, util_water_leakage → 16.1, util_cable_damage → 16.2,
  util_lighting → 16.3, util_other_damage → 16.4
  bridge_num_condition → 17.1, aesthetics_intrusion → 18.1
  maintenance_history → 19, overall_condition_visual → overall condition row

  Section 20 (maintenance recommendations): DO NOT fill any field for this — leave blank.
  The engineer fills recommendations manually.

INPUT STYLE HANDLING — Three Modes for Appendix-A Bridge Details:

Users provide bridge detail data via WhatsApp in 3 styles. You MUST correctly identify
and map values to the right JSON field in ALL THREE cases.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STYLE 1 — EXPLICIT FIELD NAMING (user states the field name, then gives the value):
  "Name of bridge is ABC Bridge, number of spans is 10, type is ROB"
  "Name of bridge: ABC Bridge. No of spans: 10. Type of bridge: ROB"
  → Map each value to the matching field using the stated name. This is straightforward.

STYLE 2 — SEQUENTIAL VALUES WITH CONNECTOR WORDS (user goes field-by-field without
  naming each field; values separated by connector words such as THEN / next / then /
  "and then" / "Next" / semicolons / commas-only with no label before the value):
  Example: "ABC Bridge THEN Not Applicable THEN NH-64 THEN 10 spans THEN ROB"
  Example: "ABC Bridge next 10 next railway over bridge"
  → Map values IN ORDER to the Appendix-A field sequence below.
  → Use semantic fit: if a value clearly doesn't match the expected positional field
    (e.g. a bridge type word appears where a number is expected), shift forward one
    position or use context to find the correct field.
  → If ANY value in the sequence carries an explicit label, treat that label as a
    positional anchor and map remaining unlabelled values relative to it.

  APPENDIX-A SEQUENTIAL FIELD ORDER (use for positional mapping in Style 2):
  Pos 01  bridge_title             Name of Bridge / Bridge Title
  Pos 02  bridge_number            Bridge Number (often absent — skip if next value is not a number/code)
  Pos 03  river_name               Name of River / Canal / Nala ("Not Applicable" for ROB)
  Pos 04  road_name                Name of Road or Highway
  Pos 05  road_number              Road Number (often absent — skip if next value is not a number)
  Pos 06  latitude                 GPS Latitude (decimal, e.g. 23.007695)
  Pos 07  longitude                GPS Longitude
  Pos 08  bm_gts_level             Location of BM / GTS Level / Arbitrary Level
  Pos 09  division                 Division Name
  Pos 10  circle                   Circle Name
  Pos 11  no_of_spans              Number of Spans (per side, e.g. "10 Nos.")
  Pos 12  total_length             Total Length of Bridge
  Pos 13  cc_of_piers              C/C of Piers (centre-to-centre spacing)
  Pos 14  width_of_piers           Width of Piers
  Pos 15  angle_of_crossing        Angle of Crossing
  Pos 16  bridge_level_type        Type: High Level / ROB / Submersible
  Pos 17  hydraulic_parameters     Hydraulic Parameters ("Not Applicable" for ROB)
  Pos 18  subsoil_particulars      Sub Soil Particulars ("As per approved GAD" or actual)
  Pos 19  superstructure_type      Type of Superstructure (PSC Girder / RCC Slab / Steel Truss)
  Pos 20  span_arrangement         Span Arrangement (full math expression, e.g. "6×25=150 m")
  Pos 21  carriage_width           Carriage Width (and footpath width if stated together)
  Pos 22  deck_level               Deck Level (in metres)
  Pos 23  foundation_type          Type of Foundation (pile / open / well)
  Pos 24  substructure_type        Type of Substructure (RCC pier / masonry abutment etc.)
  Pos 25  pier_length              Length / Height of Piers
  Pos 26  pier_width_detail        Width of Piers (detailed, per side)
  Pos 27  pier_cap_width           Width of Pier Cap
  Pos 28  abutment_width           Width of Abutment
  Pos 29  abutment_cap_width       Width of Abutment Cap
  Pos 30  returns_length           Length of Returns / Wing Walls
  Pos 31  prestressing_details     Details of Prestressing
  Pos 32  bearing_type_detail      Type of Bearings
  Pos 33  wearing_coat             Wearing Coat Type
  Pos 34  railing_type             Type of Railing / Parapet / Crash Barrier
  Pos 35  expansion_joint          Type of Expansion Joint
  Pos 36  date_of_construction_start  Date of Starting Construction (dd/mm/yyyy)
  Pos 37  date_of_completion       Date of Completion (dd/mm/yyyy)
  Pos 38  surface_utilities        Surface Utilities on Bridge
  Pos 39  design_agency            Design Agency
  Pos 40  construction_agency      Construction Agency
  Pos 41  performance              Overall Performance (Good / Fair / Poor)

STYLE 3 — PARTIAL / ABBREVIATED FIELD NAMES (user says a shortened or paraphrased
  version of the official field name; you must still map it to the correct field):
  Example: "Location of BM is 107.575 m" → bm_gts_level  (not the full label text)
  Example: "No of bridge is B-123"        → bridge_number
  Example: "Angle is Q"                   → angle_of_crossing

  Partial name used by user                       → Target JSON field
  "name of bridge" / "bridge name"                → bridge_title
  "no of bridge" / "bridge no" / "bridge number"  → bridge_number
  "name of river" / "river" / "nala name"         → river_name
  "road name" / "name of road" / "highway"        → road_name
  "road no" / "road number"                       → road_number
  "GPS" / "coordinates" / "lat lon" / "latitude"  → latitude (and longitude)
  "location of BM" / "BM level" / "GTS level"
    / "benchmark" / "BM GTS" / "arbitrary level"  → bm_gts_level
  "division" / "circle"                           → division / circle (match by context)
  "no of spans" / "number of spans" / "spans"     → no_of_spans
  "total length" / "bridge length" / "length"     → total_length
  "C/C" / "c to c" / "centre to centre" / "c/c of piers" → cc_of_piers
  "pier width" (without "cap" keyword)            → width_of_piers or pier_width_detail
  "pier cap" / "cap width" / "width of cap"       → pier_cap_width
  "abutment cap" / "abutment cap width"           → abutment_cap_width
  "abutment width" / "width of abutment"          → abutment_width
  "angle" / "skew" / "skew angle" / "crossing angle" → angle_of_crossing
  "type of bridge" / "high level or submersible"
    / "high level/ROB/submersible"                → bridge_level_type
  "hydraulic" (short form)                        → hydraulic_parameters
  "sub soil" / "subsoil" / "soil particulars"     → subsoil_particulars
  "superstructure type" / "type of superstructure" → superstructure_type
  "span arrangement" / "span details"             → span_arrangement
  "carriage" / "carriageway" / "carriage width"   → carriage_width
  "footpath" / "footpath width"                   → footpath_width (fill separately)
  "deck level" / "deck"                           → deck_level
  "foundation type" / "type of foundation"        → foundation_type
  "substructure type" / "type of substructure"    → substructure_type
  "pier length" / "height of pier" / "pier height" → pier_length
  "return walls" / "wing walls" / "returns"       → returns_length
  "prestressing" / "prestress" / "prestress details" → prestressing_details
  "bearing type" / "type of bearing" / "bearings" → bearing_type_detail
  "expansion joint" / "EJ" / "EJ type"           → expansion_joint
  "wearing coat" / "WC" / "WC type"              → wearing_coat
  "railing" / "parapet" / "crash barrier type"   → railing_type
  "date of construction" / "construction date"
    (if completion context) → date_of_completion;
    (if start context)      → date_of_construction_start
  "contractor" / "construction agency"            → construction_agency
  "designer" / "design agency"                    → design_agency
  "utilities" / "services on bridge" / "surface utilities" → surface_utilities
  "performance" / "condition" / "overall performance" → performance

COMBINED STYLES: When a message mixes explicit labels with unlabelled sequential values,
use the labelled fields as anchors and infer the positions of unlabelled values relative
to those anchors using the Pos order above. Always prefer explicit naming over positional
inference.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VOICE INSPECTION PARSING — CRITICAL RULES

Field inspectors work from a printed form. They read each row label aloud as a
self-prompt, then voice their answer. Voice-to-text captures both as one continuous
string. You must extract ONLY the ANSWER — the part that semantically resolves the
row's question — not the row label recitation.

The answer is always the concluding, semantically-final part of the input. The row
label is a prompt; the answer closes it. Do NOT strip words from the answer just
because they also appear in the label — judge by position and semantic role.

SUB-CASE 1 — Option-list row labels:
Row labels sometimes list options (e.g. "(i) Masonry, Mass Concrete, RCC" or
"high level or submersible"). The inspector reads the options while reciting the
label, then states their chosen answer last.
Rule: the LAST meaningful item is the answer.
- "Mezzanary mass concrete concrete RCC"
  → label offered "Masonry, Mass Concrete, RCC" as options
  → answer: "RCC"  (final item = chosen option)
- "type of bridge high level or submersible not applicable"
  → label offered "high level or submersible" as options
  → answer: "Not Applicable"  (conclusive phrase overrides all options)
- "type of bridge high level or submersible high level"
  → answer: "High Level"  (final item = chosen, even though it also appears in label)

SUB-CASE 2 — Parenthetical hint labels:
Some row labels contain parenthetical hints describing what to report
(e.g. "butterfly / square box / wing type etc."). These are descriptors of what
to look for, NOT part of the answer.
Rule: extract only the specific measurement, name, or description the inspector
gives; discard everything matching the parenthetical hint list in the label.
- "details of returns butterfly square box wing type 75m low garden side 57.5m ellis bridge side"
  → answer: "75 m Low Garden side + 57.5 m Ellis Bridge side"
  → do NOT include "butterfly square box wing type"

SUB-CASE 3 — Safe conclusive words (ALWAYS answers, never label content):
The following are always the inspector's answer regardless of position:
  "Not Applicable", "Data Not Available", "Not Available", "Absent", "Good",
  "Fair", "Poor", "Yes", "No", "Not Visible", any numeric measurement
  (e.g. "6.5 m", "107.575"), specific dates, place names, agency names, and any
  material name when the label has already listed the options.

SUB-CASE 4 — Consecutive-row reads in one string:
When the inspector reads two rows back-to-back, the string contains two labels
and two answers interleaved. Identify them by recognising a second row-label
phrase appearing after the first answer.
- "position of surface utilities electric lines their design details and drawings data not available"
  → surface_utilities = "Electric lines"
  → design_drawings = "Data Not Available"
  ("their design details and drawings" is the next row's label)
- "material consumed data not available"  →  material_consumed = "Data Not Available"
  (do NOT also fill material_cement / material_reinforcement etc.)
- "diagrammatic sketch give ls of bridge showing span arrangement bed profile rls ... data not applicable"
  →  ls_sketch = "Data Not Applicable"  (inspector reads row label then gives answer)
- "type of substructure masonry mass concrete RCC straight length of pier 6 m"
  → substructure_material = "RCC"  (inspector reads option list "(i) Masonry, Mass Concrete, RCC", picks last = "RCC")
  → pier_length = "6 m"  (inspector reads next row label "(ii) Straight length of pier", answers "6 m")
  → substructure_type = ""  (NOT stated; do NOT construct "RCC Straight" from material + next row title)

RULE B — One answer for multiple named rows:
When the inspector names two or more fields together before giving a single
answer, fill EVERY named field with that answer.
- "detail of prestressing and articulation not applicable"
  → prestressing_details = "Not Applicable"  AND  articulation_details = "Not Applicable"
- "total load at foundation level total horizontal force at scour level not applicable"
  → total_load_foundation = "Not Applicable"  AND  total_horizontal_force = "Not Applicable"

RULE C — "Same as above":
If the inspector says "same as above" or "same", write the literal text
"Same as above" in that field. Do NOT copy the content of the previous field.

RULE D — No photo or table references in any field:
Do NOT append "(Photo No.-X)" or "Refer Table 1/2/3/4" to any field.
All fields must contain only the inspector's plain words, exactly as stated.

RULE E — Explicit "next" row terminator:
Some inspectors say "next" (or its equivalent in their regional language, meaning
"move to the next row") between rows as a deliberate separator. When this boundary
word appears between two field answers, treat it as a hard row boundary —
everything before belongs to the current field, everything after starts a new row.
Do NOT include the boundary word itself in any field value.
- "expansion joint clogged next approach slab not applicable"
  → exp_jt_debris = "Clogged"  |  approach_slab = "Not Applicable"
- "bearing condition good next wearing coat damaged"
  → bear_pad_condition = "Good"  |  wear_coat_surface = "Damaged"
'''


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
    """Present messages to Claude grouped by section for clear context (Word format)."""
    buckets = {
        'bridge_details':  [],
        'damaged':         [],
        'observations':    [],
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
        parts.append("DAMAGE OBSERVATIONS WITH PHOTOS (use for Section B defect fields — add photo references):\n" +
                     '\n'.join(buckets['damaged']))
    if buckets['observations']:
        parts.append(
            "OBSERVATIONS WITHOUT PHOTOS (use for Section B defect fields — do NOT add photo references; "
            "write 'Observed' only; also feed into Section C recommendations where relevant):\n" +
            '\n'.join(buckets['observations'])
        )
    if buckets['recommendations']:
        parts.append("RECOMMENDATIONS (use for Section C fields):\n" +
                     '\n'.join(buckets['recommendations']))
    if buckets['general']:
        parts.append("GENERAL SITE NOTES:\n" + '\n'.join(buckets['general']))
    return '\n\n'.join(parts)


def _group_messages_by_category_excel(messages: list) -> str:
    """Present messages grouped by section for Excel format.

    Damage photo captions must NOT be used to fill any Appendix-A or Appendix-B
    field — they are labeled accordingly so Claude does not infer observations
    from them.
    """
    buckets = {
        'bridge_details':  [],
        'damaged':         [],
        'observations':    [],
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
        parts.append("BRIDGE DETAILS (use for Appendix-A fields only):\n" +
                     '\n'.join(buckets['bridge_details']))
    if buckets['damaged']:
        parts.append(
            "DAMAGE PHOTO CAPTIONS (use ONLY to generate photo_titles — "
            "do NOT use to populate any Appendix-A or Appendix-B field):\n" +
            '\n'.join(buckets['damaged'])
        )
    if buckets['observations']:
        parts.append(
            "INSPECTOR OBSERVATIONS (use for Appendix-B fields — "
            "write exactly what the inspector stated; do NOT infer or add 'Observed' "
            "unless the inspector explicitly said it):\n" +
            '\n'.join(buckets['observations'])
        )
    if buckets['recommendations']:
        parts.append("RECOMMENDATIONS:\n" + '\n'.join(buckets['recommendations']))
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

    # Use streaming — required by Anthropic SDK for requests that may exceed
    # 10 minutes (large max_tokens + photo-coord processing on Render).
    with client.messages.stream(
        model='claude-sonnet-4-6',
        max_tokens=32768,
        system=SYSTEM_PROMPT,
        messages=[{'role': 'user', 'content': user_content}]
    ) as stream:
        response = stream.get_final_message()
    raw = response.content[0].text.strip()
    stop_reason = response.stop_reason
    print(f"CLAUDE RAW RESPONSE stop_reason={stop_reason} (first 200 chars): {raw[:200]}")
    if stop_reason == 'max_tokens':
        print("WARNING: Claude hit max_tokens — JSON may be truncated; attempting repair")

    # Strip markdown code fences if Claude wrapped the JSON
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]
        raw = raw.strip()

    if not raw:
        print("ERROR: Claude returned empty response")
        raise ValueError("Claude returned empty response — check field notes content")

    result = _safe_json_parse(raw)
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

    # Locate defect coordinates in damage photos — sequential, rate-limit safe
    photo_coords = _detect_defect_coords(photo_paths, photo_descriptions, cats)
    result['photo_coords'] = [photo_coords.get(p) for p in photo_paths]

    print(f"PHOTOS injected: {photo_paths}", flush=True)
    print(f"TITLES: {result['photo_titles']}", flush=True)
    print(f"CATEGORIES: {result['photo_categories']}", flush=True)
    return result


def parse_inspection_excel(session: dict) -> dict:
    """Send session messages to Claude and return structured JSON for Excel output."""
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

    grouped_notes = _group_messages_by_category_excel(messages)

    # Assign figure numbers only to damage photos (these are the numbers used in Appendix B)
    fig_counter = 0
    photo_info  = []
    for i, (d, c) in enumerate(zip(photo_descriptions, photo_categories_from_db)):
        # NOTE: no "reference" key here — observation fields must never contain photo refs.
        # Only description and category are needed: description for title generation,
        # category to preserve the inspector's photo classification.
        entry = {"description": d, "category": c}
        if c in ('damage', 'damaged'):
            fig_counter += 1
            entry["figure_no"] = fig_counter
        else:
            entry["figure_no"] = None
        photo_info.append(entry)

    print(f"PARSE EXCEL: {len(messages)} messages, photos={len(photo_paths)}")
    print(f"GROUPED NOTES:\n{grouped_notes}")
    print(f"PHOTO INFO: {photo_info}")

    user_content = (
        f"Schema:\n{json.dumps(EXCEL_SCHEMA, indent=2)}\n\n"
        f"Field notes (grouped by section):\n{grouped_notes}\n\n"
        f"Photo information (for photo_titles and photo_categories generation ONLY — "
        f"do NOT use photo descriptions to populate any observation or data field):\n"
        f"{json.dumps(photo_info, indent=2)}\n\n"
        f"Photo file paths (in sequence order):\n{json.dumps(photo_paths)}\n\n"
        "For photo_titles: generate a short title (max 10 words) per photo from its description. "
        "photo_titles must have exactly the same count as photo file paths.\n\n"
        "For photo_categories: use the category values from Photo information — do NOT reclassify. "
        "photo_categories must have exactly the same count as photo file paths.\n\n"
        "CRITICAL: Do NOT use photo captions or photo descriptions to fill any observation field "
        "(approach_*, prot_*, waterway_*, foundations_*, sub_*, ss_*, found_*, etc.). "
        "Observation fields must contain ONLY the inspector's spoken/typed field notes. "
        "Do NOT append any photo references (e.g. '(Photo No.-1)') or table references "
        "(e.g. 'Refer Table 1') to ANY field — ever.\n\n"
        "For defect matrices: extract per-pier/span observations. Use \"\" (empty string) for any element not mentioned for a given defect type — do NOT write 'Absent'.\n\n"
    )

    # Use streaming — required by Anthropic SDK for requests that may exceed
    # 10 minutes (large max_tokens + photo-coord processing on Render).
    with client.messages.stream(
        model='claude-sonnet-4-6',
        max_tokens=32768,
        system=SYSTEM_PROMPT + EXCEL_EXTRA_PROMPT,
        messages=[{'role': 'user', 'content': user_content}]
    ) as stream:
        response = stream.get_final_message()
    raw = response.content[0].text.strip()
    stop_reason = response.stop_reason
    print(f"CLAUDE EXCEL RAW RESPONSE stop_reason={stop_reason} (first 200 chars): {raw[:200]}")
    if stop_reason == 'max_tokens':
        print("WARNING: Claude hit max_tokens — JSON may be truncated; attempting repair")

    # Strip markdown code fences if Claude wrapped the JSON
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]
        raw = raw.strip()

    if not raw:
        print("ERROR: Claude returned empty response")
        raise ValueError("Claude returned empty response — check field notes content")

    result = _safe_json_parse(raw)
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

    # Locate defect coordinates in damage photos — sequential, rate-limit safe
    photo_coords = _detect_defect_coords(photo_paths, photo_descriptions, cats)
    result['photo_coords'] = [photo_coords.get(p) for p in photo_paths]

    print(f"EXCEL PHOTOS injected: {photo_paths}", flush=True)
    print(f"TITLES: {result['photo_titles']}", flush=True)
    print(f"CATEGORIES: {result['photo_categories']}", flush=True)
    return result


def parse_inspection_amc(session: dict) -> dict:
    """Parse session for AMC Excel format.

    AMC and R&B share the same data-collection schema (pier IDs, span IDs,
    defect matrices) — the difference is purely in template layout.
    Delegates to parse_inspection_excel which already handles all of this.
    """
    return parse_inspection_excel(session)
