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

app = Flask(__name__)

# Initialise DB on startup
with app.app_context():
    init_db()


@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        # Meta verification handshake
        if request.args.get('hub.verify_token') == VERIFY_TOKEN:
            return request.args.get('hub.challenge'), 200
        return 'Forbidden', 403

    msg = parse_payload(request.json)   # extract type, phone, content, media_id

    if msg['type'] == 'audio':
        audio = download_media(msg['media_id'])
        msg['content'] = transcribe_audio(audio)
    elif msg['type'] == 'image':
        img_path = download_media(msg['media_id'], save=True)
        msg['media_path'] = img_path

    store_message(msg)                  # SQLite insert

    if 'done' in msg.get('content', '').lower():
        session = get_session(msg['phone'])
        report_json = parse_inspection(session)    # Claude API
        docx_path   = build_docx(report_json)      # python-docx
        drive_link  = upload_and_share(docx_path)  # Google Drive
        send_message(
            msg['phone'],
            f"Your report for {session['bridge']} is ready!\n{drive_link}"
        )
        mark_done(msg['phone'])

    return 'OK', 200


@app.route('/health', methods=['GET'])
def health():
    """Keep-alive endpoint for cron-job.org ping."""
    return 'CASAD Bridge Bot is running', 200


if __name__ == '__main__':
    app.run(debug=True, port=5000)
