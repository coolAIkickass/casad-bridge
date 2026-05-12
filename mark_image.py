# mark_image.py — Use Claude Vision to locate defect; returns coordinates only.
# The circle is added as an editable Word DrawingML shape in report_gen.py.
import base64, os
import anthropic

client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

_LOCATION_PROMPT = (
    'You are a bridge inspection expert. Use the caption and your knowledge '
    'of bridge components to locate the specific defect in this image.\n\n'
    'Bridge component location guide:\n'
    '- GIRDERS / I-BEAMS: horizontal members running the length of the bridge, '
    'usually the largest visible elements in the span\n'
    '- DECK SLAB: the flat top surface the vehicles drive on\n'
    '- DIAPHRAGM: vertical cross-members connecting girders laterally, '
    'perpendicular to the span direction\n'
    '- SOFFIT: the underside (ceiling) of the deck slab between girders\n'
    '- PIER / COLUMN: vertical support columns rising from the ground or water\n'
    '- PIER CAP / COPING: the horizontal beam sitting on top of the pier, '
    'directly under the girder ends\n'
    '- ABUTMENT: the end support wall at each end of the bridge where it meets the road\n'
    '- RETURN WALL / WING WALL: angled walls extending from the abutment to retain earth\n'
    '- BEARING: the rectangular pad/device between girder end and pier cap\n'
    '- EXPANSION JOINT: the gap/seal between deck sections\n'
    '- WEARING COAT: the road surface layer on top of the deck\n'
    '- PARAPET / RAILING: the safety barrier along the bridge edges\n\n'
    'Common defect appearances:\n'
    '- LEACHING / EFFLORESCENCE: white calcium deposits streaking down concrete\n'
    '- HONEYCOMBING: rough, porous concrete surface with visible voids/cavities\n'
    '- CRACK: visible line or gap in concrete surface\n'
    '- SPALLING: concrete chunks broken away, exposing rough or rebar surface\n'
    '- EXPOSED REBAR: steel reinforcement bars visible through broken concrete\n'
    '- RUST MARKS: reddish-brown staining on concrete surface\n\n'
    'Find the EXACT location of the defect described in the caption '
    'on the specific named component. '
    'Reply with ONLY two integers: x% and y% (0-100) '
    'of the defect centre from top-left. '
    'Format exactly: x,y — e.g. 45,60'
)


def get_defect_coords(img_bytes: bytes, caption: str):
    """Return (x_pct, y_pct) floats 0–1 for the defect centre, or None on failure.

    Does NOT modify the image — the caller adds an editable shape in the Word doc.
    """
    if not caption or not caption.strip():
        return None

    try:
        img_b64 = base64.standard_b64encode(img_bytes).decode()

        response = client.messages.create(
            model='claude-haiku-4-5',
            max_tokens=50,
            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'image',
                        'source': {
                            'type': 'base64',
                            'media_type': 'image/jpeg',
                            'data': img_b64,
                        },
                    },
                    {
                        'type': 'text',
                        'text': f'Inspector caption: "{caption}"\n\n{_LOCATION_PROMPT}',
                    },
                ],
            }],
        )

        raw = response.content[0].text.strip()
        x_str, y_str = raw.split(',')
        x_pct = max(0.0, min(1.0, float(x_str.strip()) / 100))
        y_pct = max(0.0, min(1.0, float(y_str.strip()) / 100))
        print(f"DEFECT COORDS: ({x_pct:.2f}, {y_pct:.2f}) for: {caption[:60]}")
        return (x_pct, y_pct)

    except Exception as e:
        print(f"GET DEFECT COORDS FAILED: {e}")
        return None
