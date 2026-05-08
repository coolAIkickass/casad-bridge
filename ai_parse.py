# ai_parse.py — Claude API: field notes → structured JSON
import json, os
import anthropic

client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

SYSTEM_PROMPT = '''
You are a structural engineering report assistant for CASAD Consultants.
Convert informal field notes into a structured JSON object matching the CASAD bridge inspection report format.
Notes may be in mixed Hindi/English or fragmented.
Output ONLY valid JSON — no markdown, no explanation, no preamble.
'''

SCHEMA = {
    "bridge_name":            "",
    "date_of_survey":         "",
    "location":               "",
    "engineer_name":          "",
    "superstructure": {
        "cracks":             "",
        "spalling":           "",
        "deflection":         "",
        "notes":              ""
    },
    "substructure": {
        "pier_condition":     "",
        "abutment_condition": "",
        "scour":              "",
        "notes":              ""
    },
    "bearings":               "",
    "expansion_joints":       "",
    "wearing_coat":           "",
    "approach":               "",
    "recommendations":        [],
    "overall_rating":         "",
    "photos":                 []
}


def parse_inspection(session: dict) -> dict:
    """Send session messages to Claude and return structured JSON."""
    messages_text = '\n'.join(
        m['content'] for m in session.get('messages', []) if m.get('content')
    )
    # Attach photo paths so Claude knows what images exist
    photo_paths = [
        m['media_path'] for m in session.get('messages', [])
        if m.get('media_path')
    ]

    user_content = (
        f"Schema:\n{json.dumps(SCHEMA, indent=2)}\n\n"
        f"Field notes:\n{messages_text}\n\n"
        f"Photo file paths (in sequence order):\n{json.dumps(photo_paths)}"
    )

    response = client.messages.create(
        model='claude-sonnet-4-20250514',
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{'role': 'user', 'content': user_content}]
    )
    raw = response.content[0].text.strip()
    return json.loads(raw)
