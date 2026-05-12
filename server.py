# server.py — CASAD Bridge Inspection Automation Pipeline
import os, threading, io
from PIL import Image
from flask import Flask, request
from dotenv import load_dotenv
from db import (init_db, store_message, get_session, get_session_status,
                get_session_state, set_session_state, increment_photo_count,
                reset_session, mark_done)
from whatsapp import parse_payload, download_media, send_message, send_document
from transcribe import transcribe_audio
from ai_parse import parse_inspection
from report_gen import build_docx

load_dotenv()

VERIFY_TOKEN = os.getenv('VERIFY_TOKEN', 'casad2024')

MENU_MSG = (
    "What would you like to share next?\n\n"
    "1️⃣  Bridge details\n"
    "2️⃣  General photos\n"
    "3️⃣  Damaged photos with observation\n"
    "4️⃣  Recommendations\n"
    "5️⃣  Generate report\n\n"
    "_Send the number to select._"
)

WELCOME_MSG = (
    "Hello CASAD team! I will assist you in creating your *bridge inspection report*.\n\n"
    + MENU_MSG
)

SECTION_NAMES = {
    '1': 'Bridge Details',
    '2': 'General Photos',
    '3': 'Damaged Photos with Observation',
    '4': 'Recommendations',
}

CATEGORY_MAP = {
    '1': 'bridge_details',
    '2': 'general',
    '3': 'damaged',
    '4': 'recommendations',
}

VALID_OPTIONS = ('1', '2', '3', '4', '5')

processed_ids = set()

app = Flask(__name__)

with app.app_context():
    init_db()


@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        if request.args.get('hub.verify_token') == VERIFY_TOKEN:
            return request.args.get('hub.challenge'), 200
        return 'Forbidden', 403

    msg = parse_payload(request.json)
    print(f"MSG RECEIVED: {msg}")

    msg_id = msg.get('msg_id')
    if msg_id and msg_id in processed_ids:
        print(f"DUPLICATE SKIPPED: {msg_id}")
        return 'OK', 200
    if msg_id:
        processed_ids.add(msg_id)

    if msg['type'] in ('unsupported', 'unknown') or msg.get('phone') == 'unknown':
        return 'OK', 200

    phone = msg['phone']
    content_raw   = (msg.get('content') or '')
    content_lower = content_raw.lower().strip()

    # ── Gratitude replies (any state) ─────────────────────────────────────────
    GRATITUDE_WORDS  = ('thank you', 'thanks', 'thankyou', 'thank u', 'shukriya', 'dhanyawad')
    GRATITUDE_EMOJIS = ('👍', '🙏', '❤️', '😊', '🤝', '👏')
    if (any(w in content_lower for w in GRATITUDE_WORDS) or
            any(e in content_raw for e in GRATITUDE_EMOJIS)):
        send_message(phone, "I am glad I could be of use to you! 😊 See you again!")
        return 'OK', 200

    status = get_session_status(phone)

    # ── New or completed session ───────────────────────────────────────────────
    if status is None or status == 'done':
        reset_session(phone)
        msg['category'] = 'system'
        store_message(msg)           # creates the session row in DB
        set_session_state(phone, 'menu')
        send_message(phone, WELCOME_MSG)
        return 'OK', 200

    state, photo_count = get_session_state(phone)

    # ── Menu state: waiting for option 1–5 ────────────────────────────────────
    if state == 'menu':
        if content_lower not in VALID_OPTIONS:
            send_message(phone,
                "Please select the correct number from below:\n\n" + MENU_MSG)
            return 'OK', 200

        if content_lower == '5':
            send_message(phone, "Generating your report now... I'll send it in a few minutes. 📄")
            threading.Thread(target=_generate_report, args=(phone,), daemon=True).start()
            return 'OK', 200

        # Valid section selected
        set_session_state(phone, content_lower)
        name = SECTION_NAMES[content_lower]
        send_message(phone,
            f"Ok! Please share *{name}*.\n\nType *done* once you are finished with this section.")
        return 'OK', 200

    # ── Collecting state: user is sharing content for a section ───────────────

    # 'done' → back to menu
    if content_lower == 'done':
        set_session_state(phone, 'menu')
        send_message(phone, "Got it! ✅\n\n" + MENU_MSG)
        return 'OK', 200

    # Process audio
    if msg['type'] == 'audio':
        audio = download_media(msg['media_id'])
        msg['content'] = transcribe_audio(audio)

    # Process image
    elif msg['type'] == 'image':
        raw_bytes = download_media(msg['media_id'])
        img = Image.open(io.BytesIO(raw_bytes))
        if img.mode in ('RGBA', 'P', 'LA'):
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=85)
        img_bytes = buf.getvalue()

        media_dir = os.getenv('MEDIA_DIR', 'media')
        os.makedirs(media_dir, exist_ok=True)
        img_path = os.path.join(media_dir, f"{msg['media_id']}.jpg")
        with open(img_path, 'wb') as f:
            f.write(img_bytes)
        msg['media_path'] = img_path
        msg['image_data'] = img_bytes

        photo_num = increment_photo_count(phone)
        msg['photo_num'] = photo_num
        section = "general" if state == '2' else "damaged"
        send_message(phone, f"📸 Photo {photo_num} saved ({section}).")

    msg['category'] = CATEGORY_MAP.get(state, 'bridge_details')
    store_message(msg)
    return 'OK', 200


def _generate_report(phone: str) -> None:
    try:
        session     = get_session(phone)
        report_json = parse_inspection(session)
        docx_path   = build_docx(report_json)
        send_document(
            phone,
            docx_path,
            caption=f"CASAD Bridge Inspection Report — {report_json.get('river_name', '')} / {report_json.get('road_name', '')}",
        )
        send_message(phone, "✅ Your inspection report is ready. Please check!")
        mark_done(phone)
    except Exception as e:
        print(f"REPORT ERROR: {e}")
        import traceback; traceback.print_exc()
        send_message(phone, f"Sorry, report generation failed: {e}")


@app.route('/health', methods=['GET'])
def health():
    return 'CASAD Bridge Bot is running', 200


@app.route('/debug/token', methods=['GET'])
def debug_token():
    import requests
    token    = os.getenv('WHATSAPP_TOKEN', '')
    phone_id = os.getenv('PHONE_NUMBER_ID', '')
    r = requests.get(
        f'https://graph.facebook.com/v19.0/{phone_id}',
        headers={'Authorization': f'Bearer {token}'}
    )
    return {
        'token_length':    len(token),
        'token_preview':   f'{token[:6]}...{token[-4:]}' if len(token) > 10 else 'TOO SHORT',
        'phone_number_id': phone_id,
        'meta_api_status': r.status_code,
        'meta_api_response': r.json()
    }, 200


if __name__ == '__main__':
    app.run(debug=True, port=5000)
