# server.py — CASAD Bridge Inspection Automation Pipeline
import os, threading, io
import concurrent.futures
from PIL import Image
from flask import Flask, request

_report_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4,
    thread_name_prefix='report-gen')
from dotenv import load_dotenv
from db import (init_db, store_message, get_session, get_session_status,
                get_session_state, set_session_state, increment_photo_count,
                reset_session, mark_done, has_bridge_details,
                set_report_format, get_report_format)
from whatsapp import parse_payload, download_media, send_message, send_document
from transcribe import transcribe_audio
from ai_parse import parse_inspection, parse_inspection_excel, parse_inspection_amc
from report_gen import build_docx
# Excel generators imported lazily inside _generate_report to avoid startup failures

load_dotenv()

VERIFY_TOKEN = os.getenv('VERIFY_TOKEN', 'casad2024')

MENU_MSG = (
    "What would you like to share next?\n\n"
    "1️⃣  Bridge details\n"
    "2️⃣  General photos\n"
    "3️⃣  Damaged photos with observation\n"
    "4️⃣  Observations (no photo)\n"
    "5️⃣  Recommendations\n"
    "6️⃣  Generate report\n"
    "7️⃣  Exit (start over / new bridge)\n\n"
    "_Send the number to select._"
)

FORMAT_SELECT_MSG = (
    "Hello CASAD team! I will assist you in creating your *bridge inspection report*.\n\n"
    "Please select your *report format*:\n\n"
    "1️⃣  Standard Word (CASAD template)\n"
    "2️⃣  Excel – R & B format\n"
    "3️⃣  Excel – AMC format\n\n"
    "_Send 1, 2 or 3 to continue._"
)

WELCOME_MSG = FORMAT_SELECT_MSG

SECTION_NAMES = {
    '1': 'Bridge Details',
    '2': 'General Photos',
    '3': 'Damaged Photos with Observation',
    '4': 'Observations (No Photo)',
    '5': 'Recommendations',
}

OBSERVATIONS_PROMPT = (
    "Ok! Please describe any *defects or damage observed* that were not captured in photos.\n\n"
    "For each defect, mention:\n"
    "• Which component is affected (e.g. pier, abutment, girder, deck slab)\n"
    "• Type of defect (e.g. cracks, leaching, honeycombing, spalling)\n"
    "• Severity or extent if known\n\n"
    "You can share multiple defects in one message, or send multiple messages / voice notes.\n\n"
    "Type *done* once you are finished with this section."
)

BRIDGE_DETAILS_PROMPT = (
    "Ok! Please share *Bridge Details* including the following:\n\n"
    "1. Name of River\n"
    "2. Name of Road\n"
    "3. Chainage of Bridge\n"
    "4. Latitude & Longitude\n"
    "5. Circle / Division / Sub-Division\n"
    "6. No. of Spans\n"
    "7. Span Length & Arrangement\n"
    "8. Type of Bridge (Simply Supported / Continuous / Arch / Other)\n"
    "9. Type of Superstructure (e.g. T-Beam, PSC Girder, Slab, Truss)\n"
    "10. Type of Substructure (e.g. RCC Pier, Masonry Abutment)\n"
    "11. Type of Foundation (e.g. Pile, Well, Open)\n"
    "12. Type of Bearing (e.g. Elastomeric, Roller, NA)\n"
    "13. Total Length of Bridge\n"
    "14. Total Length of Approach\n"
    "15. Type of Railing (RCC Parapet / Pipe Railing / Crash Barrier)\n"
    "16. River Training Work (if any)\n"
    "17. Previous Repair / Strengthening Work (if any)\n"
    "18. Clear Carriageway Width\n"
    "19. Year of Construction\n"
    "20. High Level / Submersible Bridge\n"
    "21. River — Perennial or Not\n"
    "22. Date of Survey\n\n"
    "You can share all details in one text or voice note.\n\n"
    "Type *done* once you are finished with this section."
)

CATEGORY_MAP = {
    '1': 'bridge_details',
    '2': 'general',
    '3': 'damaged',
    '4': 'observations',
    '5': 'recommendations',
}

VALID_OPTIONS = ('1', '2', '3', '4', '5', '6', '7')

processed_ids = set()

app = Flask(__name__)

try:
    with app.app_context():
        init_db()
    print("CASAD SERVER STARTED OK — DB initialised", flush=True)
except Exception as _e:
    print(f"CASAD STARTUP ERROR — DB init failed: {_e}", flush=True)
    raise


@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        token_ok = request.args.get('hub.verify_token') == VERIFY_TOKEN
        print(f"[WEBHOOK VERIFY] token_ok={token_ok}", flush=True)
        if token_ok:
            return request.args.get('hub.challenge'), 200
        return 'Forbidden', 403

    msg = parse_payload(request.json)
    print(f"[MSG IN] type={msg.get('type')} phone={msg.get('phone')} content={str(msg.get('content',''))[:60]}", flush=True)

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

    # ── Exit / restart (any state) ────────────────────────────────────────────
    if content_lower in ('7', 'exit', 'restart', 'new'):
        # If no active session or empty session, exit immediately
        if status not in ('active',):
            reset_session(phone)
            send_message(phone, "Done, hope you have a great day ahead 👋. To start again, send 'hi'.")
            return 'OK', 200
        # Active session with data — ask for confirmation
        state_now, _ = get_session_state(phone)
        set_session_state(phone, 'confirm_exit')
        send_message(phone,
            "⚠️ *Are you sure you want to exit?*\n\n"
            "All data shared in this session will be lost and you'll need to start over.\n\n"
            "• Reply *YES* to exit\n"
            "• Reply *NO* to continue where you left off")
        return 'OK', 200

    # ── New or completed session ───────────────────────────────────────────────
    if status is None or status == 'done':
        # If user typed 'done' but session was lost (e.g. Render restart wiped SQLite),
        # recover gracefully — don't restart the whole flow mid-inspection.
        if content_lower == 'done':
            send_message(phone,
                "Apologies! Your session was reset (due to server restart).\n\n"
                "Please start a new session by sending *hi* and reshare.")
            return 'OK', 200

        reset_session(phone)
        msg['category'] = 'system'
        store_message(msg)           # creates the session row in DB
        set_session_state(phone, 'format_select')
        send_message(phone, FORMAT_SELECT_MSG)
        # If they already sent a valid format selection, fall through and handle it
        # immediately rather than making them send it again.
        if content_lower not in ('1', '2', '3'):
            return 'OK', 200
        # else: fall through with state='format_select' so the handler routes them

    state, photo_count = get_session_state(phone)
    print(f"[STATE] phone={phone} state={state} photos={photo_count}", flush=True)

    # ── Confirm exit state ────────────────────────────────────────────────────
    if state == 'confirm_exit':
        if content_lower == 'yes':
            reset_session(phone)
            send_message(phone, "Done, hope you have a great day ahead 👋. To start again, send 'hi'.")
        elif content_lower == 'no':
            set_session_state(phone, 'menu')
            send_message(phone, "Welcome back! Continuing where you left off.\n\n" + MENU_MSG)
        else:
            send_message(phone, "Please reply *YES* to exit or *NO* to continue.")
        return 'OK', 200

    # ── Format selection state ─────────────────────────────────────────────────
    if state == 'format_select':
        if content_lower == '1':
            set_report_format(phone, 'word')
            set_session_state(phone, 'menu')
            send_message(phone, "✅ Standard Word format selected.\n\n" + MENU_MSG)
        elif content_lower == '2':
            set_report_format(phone, 'excel_rb')
            set_session_state(phone, 'menu')
            send_message(phone, "✅ Excel – R & B format selected.\n\n" + MENU_MSG)
        elif content_lower == '3':
            set_report_format(phone, 'excel_amc')
            set_session_state(phone, 'menu')
            send_message(phone, "✅ Excel – AMC format selected.\n\n" + MENU_MSG)
        else:
            send_message(phone, FORMAT_SELECT_MSG)
        return 'OK', 200

    # ── Confirm-generate state: user was warned about missing bridge details ────
    if state == 'confirm_generate':
        if content_lower == '6':
            set_session_state(phone, 'menu')
            send_message(phone, "Generating your report now... I'll share it in a few minutes. 📄\n\n_Note: It takes ~1 min / 5 photo to process._")
            _report_executor.submit(_generate_report, phone)
            return 'OK', 200
        elif content_lower == '1':
            set_session_state(phone, '1')
            send_message(phone, BRIDGE_DETAILS_PROMPT)
            return 'OK', 200
        else:
            send_message(phone,
                "Please send *6* to generate the report now, or *1* to add bridge details first.")
            return 'OK', 200

    # ── Menu state: waiting for option 1–5 ────────────────────────────────────
    if state == 'menu':
        if content_lower not in VALID_OPTIONS:
            send_message(phone,
                "Please select the correct number from below:\n\n" + MENU_MSG)
            return 'OK', 200

        if content_lower == '6':
            if not has_bridge_details(phone):
                send_message(phone,
                    "⚠️ *Bridge details not found.*\n\n"
                    "You haven't shared any bridge details yet (option 1️⃣). "
                    "The report will have empty fields for bridge information.\n\n"
                    "• To add bridge details first, select *1* → share details → type *done* → then select *6* again.\n"
                    "• To generate the report as-is (with missing bridge info), send *6* again now."
                )
                set_session_state(phone, 'confirm_generate')
                return 'OK', 200
            send_message(phone, "Generating your report now... I'll share it in a few minutes. 📄\n\n_Note: It takes ~1 min / 5 photo to process._")
            _report_executor.submit(_generate_report, phone)
            return 'OK', 200

        # Valid section selected
        set_session_state(phone, content_lower)
        if content_lower == '1':
            send_message(phone, BRIDGE_DETAILS_PROMPT)
        elif content_lower == '4':
            send_message(phone, OBSERVATIONS_PROMPT)
        else:
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

    # Single digit sent while in a section → guide user back to menu
    if content_lower in VALID_OPTIONS and msg['type'] == 'text':
        send_message(phone,
            "Incorrect input. To switch sections:\n\n"
            "• Type *done* to go back to the main menu.\n"
            "• Then select the menu option you want.")
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
        fmt     = get_report_format(phone)
        print(f"[REPORT START] phone={phone} fmt={fmt}", flush=True)
        session = get_session(phone)
        if fmt == 'excel_rb':
            from report_gen_excel import build_excel
            report_json = parse_inspection_excel(session)
            out_path    = build_excel(report_json)
            caption     = f"CASAD Bridge Inspection Report (R&B) — {report_json.get('bridge_title', report_json.get('river_name', ''))}"
        elif fmt == 'excel_amc':
            from report_gen_excel_amc import build_excel_amc
            report_json = parse_inspection_amc(session)
            out_path    = build_excel_amc(report_json)
            caption     = f"CASAD Bridge Inspection Report (AMC) — {report_json.get('bridge_title', report_json.get('river_name', ''))}"
        else:
            report_json = parse_inspection(session)
            out_path    = build_docx(report_json)
            caption     = f"CASAD Bridge Inspection Report — {report_json.get('river_name', '')} / {report_json.get('road_name', '')}"
        print(f"[REPORT DONE] phone={phone} fmt={fmt} path={out_path}", flush=True)
        send_document(phone, out_path, caption=caption)
        send_message(phone, "✅ Your inspection report is ready. Please check!")
        mark_done(phone, report_path=out_path)
    except Exception as e:
        import traceback
        print(f"[REPORT ERROR] phone={phone} fmt={fmt} error={e}", flush=True)
        traceback.print_exc()
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


DASHBOARD_TOKEN = os.getenv('DASHBOARD_TOKEN', 'casad-test-2024')

@app.route('/download/<path:filename>', methods=['GET'])
def download_report(filename):
    """Serve a generated report .docx for download (token-protected)."""
    from flask import send_file, abort
    if request.args.get('token') != DASHBOARD_TOKEN:
        return 'Unauthorized', 403
    # Restrict to the OUTPUT_DIR to prevent path traversal
    safe_dir  = os.path.realpath(os.getenv('OUTPUT_DIR', 'media'))
    full_path = os.path.realpath(os.path.join(safe_dir, os.path.basename(filename)))
    if not full_path.startswith(safe_dir) or not os.path.exists(full_path):
        abort(404)
    return send_file(full_path, as_attachment=True,
                     download_name=os.path.basename(full_path))

@app.route('/dashboard', methods=['GET'])
def dashboard():
    """Live HTML dashboard showing all tester sessions and their messages."""
    if request.args.get('token') != DASHBOARD_TOKEN:
        return 'Unauthorized — add ?token=YOUR_TOKEN', 403

    import sqlite3 as _sq
    from datetime import datetime as _dt, timedelta as _td

    show_all = request.args.get('all') == '1'
    token    = request.args.get('token', '')

    db  = os.getenv('DB_PATH', 'casad.db')
    con = _sq.connect(db)
    con.row_factory = _sq.Row

    if show_all:
        sessions = con.execute(
            'SELECT * FROM sessions ORDER BY started_at DESC'
        ).fetchall()
    else:
        cutoff = (_dt.utcnow() - _td(hours=3)).isoformat()
        sessions = con.execute(
            'SELECT * FROM sessions WHERE started_at >= ? ORDER BY started_at DESC',
            (cutoff,)
        ).fetchall()

    # Total count for the toggle link
    total_all = con.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]

    session_data = []
    for s in sessions:
        sid  = s['id']
        if sid:
            msgs = con.execute(
                'SELECT type, content, category, photo_num, created_at, media_path '
                'FROM messages WHERE session_id=? ORDER BY id',
                (sid,)
            ).fetchall()
            # Also grab old messages with no session_id linked to this phone
            if not msgs:
                msgs = con.execute(
                    'SELECT type, content, category, photo_num, created_at, media_path '
                    'FROM messages WHERE phone=? AND (session_id IS NULL OR session_id=?) ORDER BY id',
                    (s['phone'], sid)
                ).fetchall()
        else:
            msgs = con.execute(
                'SELECT type, content, category, photo_num, created_at, media_path '
                'FROM messages WHERE phone=? ORDER BY id',
                (s['phone'],)
            ).fetchall()
        session_data.append((dict(s), [dict(m) for m in msgs]))
    con.close()

    STATUS_COLOR = {
        'active': '#2e7d32',
        'done':   '#1565c0',
        'exited': '#e65100',
        'error':  '#c62828',
    }
    CATEGORY_ICON = {
        'bridge_details':  '🏗',
        'damaged':         '🔴',
        'observations':    '📝',
        'general':         '📷',
        'recommendations': '📋',
        'system':          '⚙️',
        None: '',
    }

    rows_html = ''
    for sess, msgs in session_data:
        phone   = sess['phone']
        status  = sess.get('status', 'active')
        state   = sess.get('state', '-')
        photos  = sess.get('photo_count', 0)
        started = (sess.get('started_at') or '')[:16].replace('T', ' ')
        ended   = (sess.get('ended_at') or '')[:16].replace('T', ' ')
        sc      = STATUS_COLOR.get(status, '#555')
        ended_html    = f'<span style="color:#999;font-size:12px">Ended {ended} UTC</span>' if ended else ''
        report_path   = sess.get('report_path')
        report_btn    = ''
        if report_path:
            fname = os.path.basename(report_path)
            if os.path.exists(report_path):
                report_btn = (f'<a href="/download/{fname}?token={token}" '
                              f'style="background:#1565c0;color:#fff;padding:4px 12px;border-radius:6px;'
                              f'font-size:12px;text-decoration:none;white-space:nowrap">⬇ Download Report</a>')
            else:
                report_btn = '<span style="color:#aaa;font-size:12px">⚠ Report file expired</span>'

        msg_rows = ''
        for m in msgs:
            mtype   = m['type'] or ''
            content = (m['content'] or '').replace('<', '&lt;').replace('>', '&gt;')
            cat     = m.get('category')
            icon    = CATEGORY_ICON.get(cat, '')
            pnum    = f' 📸{m["photo_num"]}' if m.get('photo_num') else ''
            ts      = (m.get('created_at') or '')[:16].replace('T', ' ')
            has_img = '🖼 ' if m.get('media_path') else ''
            cat_label = f'<span style="color:#888;font-size:11px">{icon} {cat or ""}{pnum}</span>' if cat else ''
            content_display = (has_img + content[:300] + ('…' if len(content) > 300 else '')) or '<em style="color:#aaa">—</em>'
            msg_rows += (
                f'<tr>'
                f'<td style="color:#888;white-space:nowrap;font-size:11px">{ts}</td>'
                f'<td><code style="background:#f0f0f0;padding:1px 4px;border-radius:3px;font-size:11px">{mtype}</code></td>'
                f'<td>{cat_label}</td>'
                f'<td style="font-size:12px">{content_display}</td>'
                f'</tr>'
            )

        rows_html += f'''
        <div style="border:1px solid #ddd;border-radius:8px;margin:16px 0;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08)">
          <div style="background:#f8f9fa;padding:10px 16px;display:flex;align-items:center;gap:16px;flex-wrap:wrap">
            <span style="font-weight:600;font-size:15px">📱 {phone}</span>
            <span style="background:{sc};color:#fff;padding:2px 8px;border-radius:12px;font-size:12px">{status}</span>
            <span style="color:#555;font-size:13px">State: <b>{state}</b></span>
            <span style="color:#555;font-size:13px">📸 {photos} photo(s)</span>
            <span style="color:#999;font-size:12px">Started {started} UTC</span>
            {ended_html}
            {report_btn}
          </div>
          <div style="overflow-x:auto">
            <table style="width:100%;border-collapse:collapse;font-size:13px">
              <thead>
                <tr style="background:#f0f4f8;text-align:left">
                  <th style="padding:6px 10px;color:#666;font-weight:600;width:130px">Time (UTC)</th>
                  <th style="padding:6px 10px;color:#666;font-weight:600;width:70px">Type</th>
                  <th style="padding:6px 10px;color:#666;font-weight:600;width:130px">Category</th>
                  <th style="padding:6px 10px;color:#666;font-weight:600">Content</th>
                </tr>
              </thead>
              <tbody>
                {msg_rows or '<tr><td colspan="4" style="padding:10px;color:#aaa;text-align:center">No messages yet</td></tr>'}
              </tbody>
            </table>
          </div>
        </div>'''

    total    = len(session_data)
    active_n = sum(1 for s, _ in session_data if s.get('status') == 'active')
    done_n   = sum(1 for s, _ in session_data if s.get('status') == 'done')
    exited_n = sum(1 for s, _ in session_data if s.get('status') == 'exited')
    now_utc  = _dt.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    if show_all:
        toggle_html = f'<a href="/dashboard?token={token}" style="color:#1565c0;font-size:13px">← Show last 3 hours only</a>'
        period_label = f'All time ({total_all} sessions)'
    else:
        toggle_html = f'<a href="/dashboard?token={token}&all=1" style="color:#1565c0;font-size:13px">Show all {total_all} sessions →</a>'
        period_label = 'Last 3 hours'

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>CASAD Test Dashboard</title>
  <style>
    body{{font-family:system-ui,sans-serif;margin:0;background:#f4f6f9;color:#222}}
    h1{{margin:0;font-size:22px}}
    .badge{{display:inline-block;padding:3px 10px;border-radius:12px;font-size:13px;font-weight:600}}
    tbody tr:nth-child(even){{background:#fafafa}}
    tbody tr:hover{{background:#f0f6ff}}
    td{{padding:6px 10px;vertical-align:top;border-top:1px solid #eee}}
  </style>
</head>
<body>
<div style="background:#1f3864;color:#fff;padding:14px 24px;display:flex;align-items:center;gap:16px">
  <h1>CASAD Bridge Bot — Test Dashboard</h1>
  <span style="margin-left:auto;font-size:13px;opacity:.7">Last loaded: {now_utc} UTC &nbsp;·&nbsp; Refresh page to update</span>
</div>
<div style="padding:16px 24px">
  <div style="display:flex;gap:12px;margin-bottom:12px;align-items:center;flex-wrap:wrap">
    <span class="badge" style="background:#e3f2fd;color:#1565c0">{period_label}: {total} sessions</span>
    <span class="badge" style="background:#e8f5e9;color:#2e7d32">Active: {active_n}</span>
    <span class="badge" style="background:#e3f2fd;color:#1565c0">Report generated: {done_n}</span>
    <span class="badge" style="background:#fff3e0;color:#e65100">Exited: {exited_n}</span>
    <span style="margin-left:auto">{toggle_html}</span>
  </div>
  {rows_html or '<p style="color:#aaa;text-align:center;margin-top:48px">No sessions in this period.</p>'}
</div>
</body>
</html>'''

    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


if __name__ == '__main__':
    app.run(debug=True, port=5000)
