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
    "Please select what you would like to share and type the number to continue:\n\n"
    "1️⃣  Bridge details\n"
    "2️⃣  General photos\n"
    "3️⃣  Damaged photos with observation\n"
    "4️⃣  Recommendations\n"
    "5️⃣  Generate report\n\n"
    "_Send the number to select an option. You can switch sections anytime by sending a different number._"
)

WELCOME_MSG = (
    "Hello CASAD team! I will assist you in creating your *bridge inspection report*.\n\n"
    + MENU_MSG
)

SECTION_PROMPTS = {
    '1': "📋 *Bridge Details*\nPlease share bridge details — name, location, road, spans, type etc. (text or voice note).\n\nSend another number anytime to switch sections.",
    '2': "📸 *General Photos*\nSend your general / overview site photos now.\n\nSend another number anytime to switch sections.",
    '3': "🔴 *Damaged Photos*\nSend damaged photos. For each photo you can:\n• Add a caption directly on the photo, OR\n• Send a text/voice note before or after (reference by photo number if describing multiple)\n\nSend another number anytime to switch sections.",
    '4': "📝 *Recommendations*\nShare your recommendations — condition rating and any remedial suggestions (text or voice note).\n\nSend another number anytime to switch sections.",
}

CATEGORY_MAP = {
    '1': 'bridge_details',
    '2': 'general',
    '3': 'damaged',
    '4': 'recommendations',
}

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
    status = get_session_status(phone)

    # New or completed session — reset and send welcome
    if status is None or status == 'done':
        reset_session(phone)
        try:
            send_message(phone, WELCOME_MSG)
            print(f"WELCOME sent to {phone}")
        except Exception as e:
            print(f"WELCOME FAILED for {phone}: {e}")
        return 'OK', 200

    state, photo_count = get_session_state(phone)
    content_raw   = (msg.get('content') or '')
    content_lower = content_raw.lower().strip()

    # ── Gratitude replies ──────────────────────────────────────────────────────
    GRATITUDE_WORDS  = ('thank you', 'thanks', 'thankyou', 'thank u', 'shukriya', 'dhanyawad')
    GRATITUDE_EMOJIS = ('👍', '🙏', '❤️', '😊', '🤝', '👏')
    if (any(w in content_lower for w in GRATITUDE_WORDS) or
            any(e in content_raw for e in GRATITUDE_EMOJIS)):
        send_message(phone, "I am glad I could be of use to you! 😊 See you again!")
        return 'OK', 200

    # ── Menu keyword ───────────────────────────────────────────────────────────
    if content_lower == 'menu':
        set_session_state(phone, 'menu')
        send_message(phone, MENU_MSG)
        return 'OK', 200

    # ── Option 1–5 selected ────────────────────────────────────────────────────
    if content_lower in ('1', '2', '3', '4', '5'):
        option = content_lower

        if option == '5':
            send_message(phone, "Generating your report now... I'll send it in a few minutes. 📄")
            threading.Thread(target=_generate_report, args=(phone,), daemon=True).start()
            return 'OK', 200

        set_session_state(phone, option)
        send_message(phone, SECTION_PROMPTS[option])
        return 'OK', 200

    # ── Content message — store under current section ──────────────────────────
    if state == 'menu':
        # User sent something without selecting a section
        send_message(phone, "Please select a section first:\n\n" + MENU_MSG)
        return 'OK', 200

    # Process media
    if msg['type'] == 'audio':
        audio = download_media(msg['media_id'])
        msg['content'] = transcribe_audio(audio)

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

        # Assign photo number and acknowledge
        photo_num = increment_photo_count(phone)
        msg['photo_num'] = photo_num
        category  = CATEGORY_MAP.get(state, 'damaged')
        section   = "general" if category == 'general' else "damaged"
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
