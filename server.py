# server.py — CASAD Bridge Inspection Automation Pipeline
import os, threading, time, io
from PIL import Image
from flask import Flask, request
from dotenv import load_dotenv
from db import init_db, store_message, get_session, get_session_status, reset_session, mark_done
from whatsapp import parse_payload, download_media, send_message, send_document
from transcribe import transcribe_audio
from ai_parse import parse_inspection
from report_gen import build_docx

load_dotenv()

VERIFY_TOKEN = os.getenv('VERIFY_TOKEN', 'casad2024')
DONE_DELAY   = int(os.getenv('DONE_DELAY_SECONDS', '20'))

WELCOME_MSG = (
    "Hello CASAD team, I will assist you in creating your *bridge inspection report*. "
    "Please share below:\n\n"
    "• Complete bridge details (name, location, type etc.)\n"
    "• Site photos - general\n"
    "• Site photos - damaged/ distressing\n"
    "• Observations about every damaged photo\n"
    "• Recommendations (if any)\n\n"
    "Type *done* when everything is sent."
)

processed_ids  = set()           # in-memory dedup
pending_cancels = {}             # phone → threading.Event

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

    # Deduplicate
    msg_id = msg.get('msg_id')
    if msg_id and msg_id in processed_ids:
        print(f"DUPLICATE SKIPPED: {msg_id}")
        return 'OK', 200
    if msg_id:
        processed_ids.add(msg_id)

    if msg['type'] in ('unsupported', 'unknown') or msg.get('phone') == 'unknown':
        return 'OK', 200

    phone = msg['phone']

    # New session detection — send welcome on first message of a new session
    status = get_session_status(phone)
    if status is None or status == 'done':
        reset_session(phone)
        try:
            send_message(phone, WELCOME_MSG)
            print(f"WELCOME sent to {phone}")
        except Exception as e:
            print(f"WELCOME FAILED for {phone}: {e}")

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

        # New photo after done → cancel pending report
        if phone in pending_cancels:
            pending_cancels[phone].set()
            send_message(phone, "More photos received! Please type *done* again when you've sent everything.")

    store_message(msg)

    content_lower = (msg.get('content') or '').lower().strip()

    # 'wait' → cancel pending report
    if 'wait' in content_lower and phone in pending_cancels:
        pending_cancels[phone].set()
        send_message(
            phone,
            "Ok, I am waiting. Please share remaining info. "
            "Once done, type *done* to generate report."
        )
        return 'OK', 200

    # 'done' → schedule report with delay
    if 'done' in content_lower:
        cancel_event = threading.Event()
        pending_cancels[phone] = cancel_event
        send_message(
            phone,
            "Good job! Generating your report in few minutes. I will notify you once done.\n\n"
            "Note: Make sure all photos/info is sent, *if not, please type 'wait'.*"
        )
        threading.Thread(target=_generate_report, args=(phone, cancel_event), daemon=True).start()

    return 'OK', 200


def _generate_report(phone: str, cancel_event: threading.Event) -> None:
    print(f"REPORT: waiting {DONE_DELAY}s for pending media from {phone}...")
    time.sleep(DONE_DELAY)

    if cancel_event.is_set():
        print(f"REPORT CANCELLED for {phone}")
        pending_cancels.pop(phone, None)
        return

    pending_cancels.pop(phone, None)

    try:
        session     = get_session(phone)
        report_json = parse_inspection(session)
        docx_path   = build_docx(report_json)
        send_document(
            phone,
            docx_path,
            caption=f"CASAD Bridge Inspection Report - {report_json.get('river_name', '')} / {report_json.get('road_name', '')}",
        )
        send_message(phone, "Yay! Your inspection report is generated, please check.")
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
