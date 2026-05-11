# mark_image.py — Use Claude Vision to locate defect, draw circle with Pillow
import base64, io, os
import anthropic
from PIL import Image, ImageDraw

client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))


def mark_defect(img_bytes: bytes, caption: str) -> bytes:
    """
    Ask Claude where the defect is, draw a red circle on the image.
    Returns original bytes unchanged if no caption or if detection fails.
    """
    if not caption or not caption.strip():
        return img_bytes

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
                        'text': (
                            f'Inspector noted: "{caption}". '
                            'Where is the defect/damage in this image? '
                            'Reply with ONLY two integers: x% and y% (0-100) '
                            'of the defect centre from top-left. '
                            'Format exactly: x,y — e.g. 45,60'
                        ),
                    },
                ],
            }],
        )

        coords = response.content[0].text.strip()
        x_str, y_str = coords.split(',')
        x_pct = float(x_str.strip()) / 100
        y_pct = float(y_str.strip()) / 100

        img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
        w, h = img.size
        cx = int(x_pct * w)
        cy = int(y_pct * h)
        radius = max(30, min(w, h) // 8)

        draw = ImageDraw.Draw(img)
        draw.ellipse(
            [(cx - radius, cy - radius), (cx + radius, cy + radius)],
            outline='red',
            width=max(4, min(w, h) // 100),
        )

        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=85)
        print(f"DEFECT MARKED at ({cx},{cy}) r={radius} for: {caption[:60]}")
        return buf.getvalue()

    except Exception as e:
        print(f"MARK DEFECT FAILED: {e} — using original image")
        return img_bytes
