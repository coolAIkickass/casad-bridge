# mark_image.py — Use Claude Vision to locate defect; returns coordinates only.
# The circle is added as an editable Word DrawingML shape in report_gen.py.
import base64, os, re, time
import anthropic

client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

_SYSTEM_PROMPT = (
    'You are a bridge inspection tool. '
    'Your output must be ONLY two integers in the format x,y — nothing else. '
    'No analysis, no explanation, no markdown. Just digits and a comma.'
)

_LOCATION_CONTEXT = (
    'Bridge component guide:\n'
    '- GIRDERS/I-BEAMS: horizontal members running the span length\n'
    '- DECK SLAB: flat top surface vehicles drive on\n'
    '- DIAPHRAGM: vertical cross-members connecting girders laterally\n'
    '- SOFFIT: underside of the deck slab between girders\n'
    '- PIER/COLUMN: vertical support columns\n'
    '- PIER CAP/COPING: horizontal beam on top of pier under girder ends\n'
    '- ABUTMENT: end support wall where bridge meets road\n'
    '- BEARING: pad between girder end and pier cap\n'
    '- EXPANSION JOINT: gap/seal between deck sections\n'
    '- PARAPET/RAILING: safety barrier along bridge edges\n\n'
    'Common defects: leaching (white streaks), honeycombing (porous voids), '
    'crack (visible line), spalling (broken concrete), exposed rebar, rust marks.'
)

# Retry delays for 529 overloaded errors: 30s, 60s, 120s
_OVERLOAD_RETRY_DELAYS = [30, 60, 120]


def get_defect_coords(img_bytes: bytes, caption: str):
    """Return (x_pct, y_pct) floats 0–1 for the defect centre, or None on failure.

    Retries up to 3 times on 529 overloaded errors with increasing back-off.
    Does NOT modify the image — the caller adds an editable shape in the report.
    """
    if not caption or not caption.strip():
        return None

    img_b64 = base64.standard_b64encode(img_bytes).decode()

    for attempt in range(len(_OVERLOAD_RETRY_DELAYS) + 1):
        try:
            response = client.messages.create(
                model='claude-haiku-4-5',
                max_tokens=15,
                system=_SYSTEM_PROMPT,
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
                            'text': (
                                f'{_LOCATION_CONTEXT}\n\n'
                                f'Caption: "{caption}"\n\n'
                                f'Where is the defect centre? Reply x,y (0-100 from top-left).'
                            ),
                        },
                    ],
                }],
            )

            raw = response.content[0].text.strip()
            # Extract the first two integers — handles extra words or punctuation
            # Claude occasionally adds despite the strict prompt.
            nums = re.findall(r'\d+', raw)
            if len(nums) < 2:
                print(f"DEFECT COORDS: unexpected response {raw!r} for: {caption[:60]}")
                return None
            x_pct = max(0.0, min(1.0, float(nums[0]) / 100))
            y_pct = max(0.0, min(1.0, float(nums[1]) / 100))
            print(f"DEFECT COORDS: ({x_pct:.2f}, {y_pct:.2f}) for: {caption[:60]}")
            return (x_pct, y_pct)

        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < len(_OVERLOAD_RETRY_DELAYS):
                wait = _OVERLOAD_RETRY_DELAYS[attempt]
                print(f"DEFECT COORDS: API overloaded (529), "
                      f"retry {attempt + 1}/{len(_OVERLOAD_RETRY_DELAYS)} in {wait}s "
                      f"for: {caption[:50]}")
                time.sleep(wait)
                continue
            print(f"GET DEFECT COORDS FAILED: {e}")
            return None

        except Exception as e:
            print(f"GET DEFECT COORDS FAILED: {e}")
            return None

    return None
