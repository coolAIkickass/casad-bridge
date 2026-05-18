#!/usr/bin/env python3
"""
test_sim.py — CASAD Bot Local Simulator
Tests the full bot flow locally — no WhatsApp credentials required.

USAGE
-----
  python test_sim.py                        # interactive chat (default)
  python test_sim.py --auto                 # run full scripted scenario (Word)
  python test_sim.py --auto --fmt 2         # auto scenario — Excel R&B
  python test_sim.py --auto --fmt 3         # auto scenario — Excel AMC
  python test_sim.py --reset                # wipe test DB and start fresh
  python test_sim.py --phone 919911111111   # use custom test phone number

INTERACTIVE COMMANDS
--------------------
  <text>                   send a text message
  img:<path>               send a photo  (e.g.  img:/tmp/bridge.jpg)
  img:<path> | <caption>   send a photo with caption/observation
  voice:<path>             send an audio file (will be transcribed)
  /reset                   wipe this phone's session and start over
  /status                  show current session state
  /quit  or  Ctrl-C        exit the simulator
"""

import sys, os, json, time, io, shutil, argparse, threading
from contextlib import ExitStack
from unittest.mock import patch, MagicMock
from pathlib import Path

# ── Environment setup BEFORE any project imports ────────────────────────────
os.environ.setdefault('DB_PATH',              'test_casad.db')
os.environ.setdefault('MEDIA_DIR',            'media')
os.environ.setdefault('EXCEL_TEMPLATE_PATH',  'casad_excel_template.xlsx')
os.environ.setdefault('AMC_TEMPLATE_PATH',    'casad_amc_template.xlsx')
os.environ.setdefault('WHATSAPP_TOKEN',       'sim_fake_token')
os.environ.setdefault('PHONE_NUMBER_ID',      'sim_fake_phone_id')
os.environ.setdefault('VERIFY_TOKEN',         'casad2024')
os.environ.setdefault('GROQ_API_KEY',         'sim_fake_groq_key')
# Use .env for real API keys (Anthropic, etc.) if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Colours ──────────────────────────────────────────────────────────────────
RESET  = '\033[0m'
BOLD   = '\033[1m'
GREEN  = '\033[32m'
CYAN   = '\033[36m'
YELLOW = '\033[33m'
RED    = '\033[31m'
DIM    = '\033[2m'
BLUE   = '\033[34m'

def _c(text, *codes): return ''.join(codes) + str(text) + RESET
def bot(msg):  print(_c(f'🤖  {msg}', CYAN))
def user(msg): print(_c(f'👤  {msg}', GREEN))
def info(msg): print(_c(f'ℹ   {msg}', YELLOW))
def doc(msg):  print(_c(f'📎  {msg}', BLUE, BOLD))
def err(msg):  print(_c(f'❌  {msg}', RED))
def hr():      print(_c('─' * 60, DIM))

# ── Message capture ──────────────────────────────────────────────────────────
_lock = threading.Lock()
_pending_bot_msgs = []   # text messages queued by background thread
_pending_bot_docs = []   # documents queued by background thread
_report_done = threading.Event()  # set when report finishes (ready/failed/sorry)

def _capture_send_message(phone, text):
    with _lock:
        _pending_bot_msgs.append(text)
    # Signal completion when the bot says report is ready or failed
    lower = text.lower()
    if any(w in lower for w in ('ready', 'failed', 'sorry', 'error')):
        _report_done.set()

def _capture_send_document(phone, file_path, caption=''):
    with _lock:
        _pending_bot_docs.append((file_path, caption))
    _report_done.set()   # document sent = report complete

def _flush_bot_output():
    """Print anything the bot sent since last flush."""
    with _lock:
        msgs = list(_pending_bot_msgs); _pending_bot_msgs.clear()
        docs = list(_pending_bot_docs); _pending_bot_docs.clear()
    for text in msgs:
        bot(text)
    for fpath, caption in docs:
        doc(f'Document sent: {fpath}')
        if caption:
            doc(f'Caption: {caption}')
        size_kb = os.path.getsize(fpath) // 1024 if os.path.exists(fpath) else 0
        info(f'File size: {size_kb} KB  |  path: {fpath}')

# ── Fake media registry ───────────────────────────────────────────────────────
_fake_media: dict[str, bytes] = {}   # fake_media_id -> raw bytes
_fake_media_type: dict[str, str] = {}

def _register_fake_image(path: str) -> str:
    """Store image bytes under a fake media ID and return that ID."""
    import uuid
    mid = f'sim_{uuid.uuid4().hex[:8]}'
    with open(path, 'rb') as f:
        _fake_media[mid] = f.read()
    _fake_media_type[mid] = 'image/jpeg'
    return mid

def _register_fake_audio(path: str) -> str:
    import uuid
    mid = f'sim_audio_{uuid.uuid4().hex[:8]}'
    with open(path, 'rb') as f:
        _fake_media[mid] = f.read()
    _fake_media_type[mid] = 'audio/ogg; codecs=opus'
    return mid

def _mock_download_media(media_id: str, save: bool = False):
    raw = _fake_media.get(media_id, b'')
    if save:
        ext = _fake_media_type.get(media_id, 'image/jpeg').split('/')[1].split(';')[0]
        path = os.path.join(os.getenv('MEDIA_DIR', 'media'), f'{media_id}.{ext}')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(raw)
        return path
    return raw

# ── Payload builders ─────────────────────────────────────────────────────────
_msg_counter = 0
def _next_id():
    global _msg_counter
    _msg_counter += 1
    return f'sim_msg_{_msg_counter:04d}'

def _text_payload(phone, text):
    return {'entry': [{'changes': [{'value': {
        'messages': [{'from': phone, 'id': _next_id(), 'type': 'text',
                      'text': {'body': text}}]
    }}]}]}

def _image_payload(phone, media_id, caption=''):
    return {'entry': [{'changes': [{'value': {
        'messages': [{'from': phone, 'id': _next_id(), 'type': 'image',
                      'image': {'id': media_id, 'caption': caption}}]
    }}]}]}

def _audio_payload(phone, media_id):
    return {'entry': [{'changes': [{'value': {
        'messages': [{'from': phone, 'id': _next_id(), 'type': 'audio',
                      'audio': {'id': media_id}}]
    }}]}]}

# ── Import server (with mocks active) ────────────────────────────────────────
def _build_app(use_mock_ai: bool = False):
    """Import the Flask app with WhatsApp I/O and external clients mocked out."""
    # Mock Groq client before transcribe.py initialises it at module level
    mock_groq = MagicMock()
    with patch.dict('sys.modules', {'groq': MagicMock(Groq=MagicMock(return_value=mock_groq))}):
        import server as _srv
        import whatsapp as _wa

    # Permanently replace WhatsApp I/O in the imported modules
    # (must be permanent so background threads in _generate_report also see the mock)
    _wa.send_message  = _capture_send_message
    _wa.send_document = _capture_send_document
    _wa.download_media = _mock_download_media
    # Also patch references in server module (it imports these at top level)
    import server as _srv2
    _srv2.send_message  = _capture_send_message
    _srv2.send_document = _capture_send_document

    # Permanently replace AI parsers when --mock-ai is set
    if use_mock_ai:
        _srv2.parse_inspection       = _mock_parse_inspection
        _srv2.parse_inspection_excel = _mock_parse_inspection_excel
        _srv2.parse_inspection_amc   = _mock_parse_inspection_amc

    return _srv2.app

# ── Mock AI parse result (used with --mock-ai) ───────────────────────────────
MOCK_REPORT_JSON = {
    'river_name':          'Surya River',
    'road_name':           'Raval-Waghodiya Road',
    'bridge_title':        'Surya River Bridge',
    'chainage':            '14+200',
    'latitude':            '22.1234 N',
    'longitude':           '73.4567 E',
    'circle':              'Baroda',
    'division':            'Vadodara Division',
    'no_of_spans':         '5',
    'total_length':        '60 m',
    'span_length':         '12 m x 5',
    'bridge_type':         'Simply Supported',
    'superstructure_type': 'T-Beam Girder',
    'foundation_type':     'Open Foundation',
    'bearing_type_detail': 'Elastomeric Bearing',
    'wearing_coat':        'Bituminous concrete, minor cracks near kerb',
    'railing_type':        'RCC Parapet',
    'year_of_construction':'1998',
    'date_of_survey':      '14/04/2026',
    'project_number':      'TEST-001',
    'sub_cracks':          'Absent',
    'ss_cracks':           'Present — minor transverse cracks on span 3',
    'ss_spalling':         'Absent',
    'ss_exposed_rebar':    'Absent',
    'approach_settlement': 'Absent',
    'approach_erosion':    'Absent',
    'sub_piers_side1':     ['A1', 'P1', 'P2', 'P3', 'P4', 'A2'],
    'sub_piers_side2':     ['A1', 'P1', 'P2', 'P3', 'P4', 'A2'],
    'super_spans_side1':   ['S1', 'S2', 'S3', 'S4', 'S5'],
    'super_spans_side2':   ['S1', 'S2', 'S3', 'S4', 'S5'],
    'defect_sub1':  {'P1': {'cracks': 'Absent'}, 'P2': {'cracks': 'Present — moderate'}},
    'defect_sub2':  {'P1': {'cracks': 'Absent'}, 'P2': {'cracks': 'Absent'}},
    'defect_super1':{'S3': {'cracks': 'Present — minor', 'honeycombing': 'Present'}},
    'defect_super2':{'S3': {'cracks': 'Absent'}},
    'recommendations': [
        'Epoxy injection for pier cracks',
        'Patch honeycombing with micro-concrete',
        'Vegetation removal around pier bases',
    ],
    'photos':           [],
    'photo_titles':     [],
    'photo_categories': [],
}

_USE_MOCK_AI = False   # set to True via --mock-ai flag

def _mock_parse_inspection(session):
    info('[MOCK AI] Skipping Claude API — returning preset report_json')
    return dict(MOCK_REPORT_JSON)

def _mock_parse_inspection_excel(session):
    return _mock_parse_inspection(session)

def _mock_parse_inspection_amc(session):
    return _mock_parse_inspection(session)

# ── Core send helper ─────────────────────────────────────────────────────────
def _send(client, phone, payload, wait_for_bg=False):
    """POST payload to /webhook and print bot responses.
    WhatsApp + AI mocks are permanently applied at startup — no patch context needed here.
    """
    client.post('/webhook', json=payload, content_type='application/json')
    if wait_for_bg:
        info('Generating report… (this may take a while with real photos)')
        _report_done.clear()
        # Wait up to 10 min; flush output every 3 s so user sees live progress
        deadline = time.time() + 600
        while time.time() < deadline:
            fired = _report_done.wait(timeout=3)
            _flush_bot_output()
            if fired:
                _report_done.clear()
                break
    else:
        time.sleep(0.1)   # let Flask finish
        _flush_bot_output()

# ── Interactive mode ──────────────────────────────────────────────────────────
def run_interactive(client, phone):
    print()
    print(_c('CASAD Bot Simulator — Interactive Mode', BOLD))
    print(_c(f'Phone: {phone}   DB: {os.environ["DB_PATH"]}', DIM))
    print(_c('Type a message, or use img:<path>, voice:<path>, /reset, /status, /quit', DIM))
    hr()

    # Kick off the session
    _send(client, phone, _text_payload(phone, 'hi'))

    while True:
        try:
            raw = input(_c('\nYou: ', GREEN, BOLD)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            info('Exiting simulator.')
            break

        if not raw:
            continue

        user(raw)

        # ── Simulator commands ────────────────────────────────────────────
        if raw == '/quit':
            info('Exiting simulator.')
            break

        if raw == '/reset':
            import db
            db.reset_session(phone)
            info('Session reset. Sending hi to restart…')
            _send(client, phone, _text_payload(phone, 'hi'))
            continue

        if raw == '/status':
            import db
            state, photos = db.get_session_state(phone)
            status = db.get_session_status(phone)
            fmt    = db.get_report_format(phone)
            info(f'status={status}  state={state}  photos={photos}  format={fmt}')
            continue

        # ── Image input  img:/path/to/file.jpg  or  img:/path | caption ──
        if raw.startswith('img:'):
            parts   = raw[4:].split('|', 1)
            path    = parts[0].strip()
            caption = parts[1].strip() if len(parts) > 1 else ''
            if not os.path.exists(path):
                err(f'File not found: {path}')
                continue
            mid = _register_fake_image(path)
            _send(client, phone, _image_payload(phone, mid, caption))
            continue

        # ── Audio input  voice:/path/to/file.ogg ──────────────────────────
        if raw.startswith('voice:'):
            path = raw[6:].strip()
            if not os.path.exists(path):
                err(f'File not found: {path}')
                continue
            mid = _register_fake_audio(path)
            _send(client, phone, _audio_payload(phone, mid))
            continue

        # ── Generate report (option 6) — wait for background thread ───────
        is_generate = raw.strip() == '6'
        import db
        state, _ = db.get_session_state(phone)
        if is_generate or (state == 'confirm_generate' and raw.strip() == '6'):
            _send(client, phone, _text_payload(phone, raw), wait_for_bg=True)
        else:
            _send(client, phone, _text_payload(phone, raw))

# ── Automated scenario ────────────────────────────────────────────────────────
BRIDGE_DETAILS_TEXT = (
    "River: Surya River\n"
    "Road: Raval-Waghodiya Road\n"
    "Chainage: 14+200\n"
    "Latitude: 22.1234 N, Longitude: 73.4567 E\n"
    "Circle: Baroda\n"
    "Division: Vadodara Division\n"
    "No. of Spans: 5\n"
    "Span Length: 12m x 5 = 60m\n"
    "Bridge Type: Simply Supported\n"
    "Superstructure: T-Beam Girder\n"
    "Substructure: RCC Pier and Abutment\n"
    "Foundation: Open Foundation\n"
    "Bearing: Elastomeric Bearing\n"
    "Total Length: 60m\n"
    "Railing: RCC Parapet\n"
    "Year of Construction: 1998\n"
    "Date of Survey: 14/04/2026"
)

DAMAGE_OBSERVATION = "Heavy cracking observed on Pier 2 at mid-height. Honeycombing visible on underside of Span 3 girder. Leaching from abutment joints."
GENERAL_OBSERVATION = "Some minor vegetation growth noted near pier bases. Expansion joints appear in good condition. Wearing coat shows minor cracks near kerb."
RECOMMENDATIONS_TEXT = "1. Immediate repair of pier cracks with epoxy injection. 2. Patch honeycombing with micro-concrete. 3. Vegetation removal around pier bases."


def run_auto(client, phone, fmt_choice: str = '1', sample_img_path: str = None):
    """Run a full scripted inspection scenario end-to-end."""
    print()
    print(_c('CASAD Bot Simulator — Automated Test', BOLD))
    print(_c(f'Phone: {phone}   Format: {fmt_choice}   DB: {os.environ["DB_PATH"]}', DIM))
    hr()

    steps = [
        # (description, payload_or_callable)
        ('Start session', lambda: _text_payload(phone, 'hi')),
        (f'Select format {fmt_choice}', lambda: _text_payload(phone, fmt_choice)),
        ('Select option 1 (Bridge details)', lambda: _text_payload(phone, '1')),
        ('Share bridge details', lambda: _text_payload(phone, BRIDGE_DETAILS_TEXT)),
        ('Done — back to menu', lambda: _text_payload(phone, 'done')),
    ]

    if sample_img_path:
        def _general_photo():
            mid = _register_fake_image(sample_img_path)
            return _image_payload(phone, mid, 'General view of bridge')
        def _damage_photo():
            mid = _register_fake_image(sample_img_path)
            return _image_payload(phone, mid, DAMAGE_OBSERVATION)

        steps += [
            ('Select option 2 (General photos)', lambda: _text_payload(phone, '2')),
            ('Send general photo', _general_photo),
            ('Done general photos', lambda: _text_payload(phone, 'done')),
            ('Select option 3 (Damaged photos)', lambda: _text_payload(phone, '3')),
            ('Send damage photo with obs', _damage_photo),
            ('Done damaged photos', lambda: _text_payload(phone, 'done')),
        ]

    steps += [
        ('Select option 4 (Observations)', lambda: _text_payload(phone, '4')),
        ('Share observations', lambda: _text_payload(phone, GENERAL_OBSERVATION)),
        ('Done observations', lambda: _text_payload(phone, 'done')),
        ('Select option 5 (Recommendations)', lambda: _text_payload(phone, '5')),
        ('Share recommendations', lambda: _text_payload(phone, RECOMMENDATIONS_TEXT)),
        ('Done recommendations', lambda: _text_payload(phone, 'done')),
    ]

    total = len(steps) + 1  # +1 for generate step
    for i, (desc, payload_fn) in enumerate(steps, 1):
        print(f'\n{_c(f"[{i}/{total}]", DIM, BOLD)} {_c(desc, YELLOW)}')
        payload = payload_fn()
        # Show what "user" is sending
        if payload['entry'][0]['changes'][0]['value']['messages'][0]['type'] == 'text':
            txt = payload['entry'][0]['changes'][0]['value']['messages'][0]['text']['body']
            if len(txt) > 80:
                user(txt[:80] + '…')
            else:
                user(txt)
        else:
            user(f'[{payload["entry"][0]["changes"][0]["value"]["messages"][0]["type"]} message]')
        _send(client, phone, payload)
        time.sleep(0.2)

    # Generate report
    print(f'\n{_c(f"[{total}/{total}]", DIM, BOLD)} {_c("Generate report (option 6)", YELLOW)}')
    user('6')
    _send(client, phone, _text_payload(phone, '6'), wait_for_bg=True)

    hr()
    print(_c('Automated test complete.', BOLD, GREEN))


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='CASAD Bot Simulator')
    parser.add_argument('--auto',     action='store_true', help='Run automated test scenario')
    parser.add_argument('--mock-ai',  action='store_true', help='Skip Claude API — use preset report_json (fast, no API key needed)')
    parser.add_argument('--fmt',      default='1',         help='Format: 1=Word  2=Excel R&B  3=Excel AMC')
    parser.add_argument('--phone',    default='919900000001', help='Test phone number')
    parser.add_argument('--img',      default=None,        help='Sample image path for auto-test')
    parser.add_argument('--reset',    action='store_true', help='Wipe test DB and exit')
    args = parser.parse_args()

    global _USE_MOCK_AI
    _USE_MOCK_AI = args.mock_ai
    if _USE_MOCK_AI:
        info('Mock AI mode ON — Claude API will be bypassed')

    if args.reset:
        db_path = os.environ['DB_PATH']
        if os.path.exists(db_path):
            os.remove(db_path)
            info(f'Deleted test DB: {db_path}')
        else:
            info(f'No test DB found at {db_path}')
        return

    # Ensure test DB is separate from production
    assert os.environ['DB_PATH'] != 'casad.db', \
        'DB_PATH must not be casad.db in test mode!'

    # Re-init DB fresh for each test run (keeps production DB untouched)
    db_path = os.environ['DB_PATH']
    if os.path.exists(db_path):
        info(f'Reusing existing test DB: {db_path}')
    else:
        info(f'Creating fresh test DB: {db_path}')

    # Build app (imports server, inits DB)
    info('Loading server…')
    app = _build_app(use_mock_ai=_USE_MOCK_AI)
    client = app.test_client()
    info('Server ready.\n')

    if args.auto:
        img = args.img
        if img and not os.path.exists(img):
            err(f'--img path not found: {img}')
            sys.exit(1)
        run_auto(client, args.phone, fmt_choice=args.fmt, sample_img_path=img)
    else:
        run_interactive(client, args.phone)


if __name__ == '__main__':
    main()
