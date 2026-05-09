# server.py — CASAD Bridge Inspection Automation Pipeline
import os
from flask import Flask, request
from dotenv import load_dotenv
from db import init_db, store_message, get_session, mark_done
from whatsapp import parse_payload, download_media, send_message
from transcribe import transcribe_audio
from ai_parse import parse_inspection
from report_gen import build_docx
from drive import upload_and_share

load_dotenv()

VERIFY_TOKEN = os.getenv('VERIFY_TOKEN', 'casad2024')
processed_ids = set()  # in-memory dedup cache

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

    # Deduplicate — Meta sometimes delivers the same message 2-3 times
    msg_id = msg.get('msg_id')
    if msg_id and msg_id in processed_ids:
        print(f"DUPLICATE SKIPPED: {msg_id}")
        return 'OK', 200
    if msg_id:
        processed_ids.add(msg_id)

    if msg['type'] == 'unsupported':
        print(f"UNSUPPORTED TYPE SKIPPED")
        return 'OK', 200

    if msg['type'] == 'audio':
        audio = download_media(msg['media_id'])
        msg['content'] = transcribe_audio(audio)
    elif msg['type'] == 'image':
        img_path = download_media(msg['media_id'], save=True)
        msg['media_path'] = img_path

    store_message(msg)

    if 'done' in (msg.get('content') or '').lower():
        session = get_session(msg['phone'])
        report_json = parse_inspection(session)
        docx_path   = build_docx(report_json)
        drive_link  = upload_and_share(docx_path)
        send_message(
            msg['phone'],
            f"Your report for {session['bridge']} is ready!\n{drive_link}"
        )
        mark_done(msg['phone'])

    return 'OK', 200


@app.route('/health', methods=['GET'])
def health():
    return 'CASAD Bridge Bot is running', 200


@app.route('/debug/token', methods=['GET'])
def debug_token():
    import requests
    token = os.getenv('WHATSAPP_TOKEN', '')
    phone_id = os.getenv('PHONE_NUMBER_ID', '')
    r = requests.get(
        f'https://graph.facebook.com/v19.0/{phone_id}',
        headers={'Authorization': f'Bearer {token}'}
    )
    return {
        'token_length': len(token),
        'token_preview': f'{token[:6]}...{token[-4:]}' if len(token) > 10 else 'TOO SHORT',
        'phone_number_id': phone_id,
        'meta_api_status': r.status_code,
        'meta_api_response': r.json()
    }, 200


if __name__ == '__main__':
    app.run(debug=True, port=5000)