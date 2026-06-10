# ed_blueprint.py — ED Checker (Drawing Review) app mounted at /ed
import os
import uuid
import logging
import psycopg2
import psycopg2.extras
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, Response
import json
from ed_checker import run_check, parse_design_inputs

# Ensure Python logs reach Render's stdout
logging.basicConfig(
    level=logging.INFO,
    format='[ED %(levelname)s] %(name)s — %(message)s',
    force=True,
)

ed_bp = Blueprint(
    'ed',
    __name__,
    template_folder='ed_templates',
    static_folder='ed_static',
)

DATABASE_URL = os.environ.get('DATABASE_URL', '')


def _get_db():
    url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def init_ed_db():
    """Create ED Checker tables if they don't exist. Called from main.py at startup."""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS drawings (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            drawing_type TEXT NOT NULL DEFAULT 'General',
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reviews (
            id           TEXT PRIMARY KEY,
            drawing_id   TEXT NOT NULL REFERENCES drawings(id),
            version      INTEGER NOT NULL DEFAULT 1,
            pdf_content  BYTEA NOT NULL,
            status       TEXT NOT NULL DEFAULT 'complete',
            created_at   TEXT NOT NULL
        );
        ALTER TABLE reviews ADD COLUMN IF NOT EXISTS design_data TEXT;
        CREATE TABLE IF NOT EXISTS issues (
            id          TEXT PRIMARY KEY,
            review_id   TEXT NOT NULL REFERENCES reviews(id),
            category    TEXT NOT NULL,
            title       TEXT NOT NULL,
            description TEXT NOT NULL,
            suggestion  TEXT,
            severity    TEXT NOT NULL DEFAULT 'error',
            page_num    INTEGER NOT NULL DEFAULT 1,
            x           REAL DEFAULT 10,
            y           REAL DEFAULT 10,
            width       REAL DEFAULT 20,
            height      REAL DEFAULT 10,
            status      TEXT NOT NULL DEFAULT 'open'
        );
    ''')
    conn.commit()
    cur.close()
    conn.close()
    print("ED Checker DB initialised", flush=True)


def _save_issues(review_id, issues, cur):
    for issue in issues:
        cur.execute(
            '''INSERT INTO issues
               (id, review_id, category, title, description, suggestion,
                severity, page_num, x, y, width, height, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open')''',
            (str(uuid.uuid4()), review_id,
             issue.get('category', 'General'),
             issue.get('title', 'Issue'),
             issue.get('description', ''),
             issue.get('suggestion', ''),
             issue.get('severity', 'error'),
             issue.get('page_num', 1),
             issue.get('x', 5), issue.get('y', 5),
             issue.get('width', 20), issue.get('height', 10))
        )


@ed_bp.route('/')
def index():
    per_page = 10
    page     = max(1, request.args.get('page', 1, type=int))
    offset   = (page - 1) * per_page

    conn = _get_db()
    cur = conn.cursor()

    cur.execute('SELECT COUNT(*) AS total FROM reviews r JOIN drawings d ON d.id = r.drawing_id')
    total      = cur.fetchone()['total']
    total_pages = max(1, -(-total // per_page))   # ceiling division

    cur.execute('''
        SELECT r.id AS review_id, d.id AS drawing_id, d.name, d.drawing_type,
               r.version, r.status, r.created_at,
               COUNT(CASE WHEN i.severity='error'   AND i.status='open' THEN 1 END) AS open_errors,
               COUNT(CASE WHEN i.severity='warning' AND i.status='open' THEN 1 END) AS open_warnings,
               COUNT(CASE WHEN i.status='open' THEN 1 END) AS open_total
        FROM reviews r
        JOIN drawings d ON d.id = r.drawing_id
        LEFT JOIN issues i ON i.review_id = r.id
        GROUP BY r.id, d.id, d.name, d.drawing_type, r.version, r.status, r.created_at
        ORDER BY r.created_at DESC
        LIMIT %s OFFSET %s
    ''', (per_page, offset))
    recent = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('upload.html', recent=recent,
                           page=page, total_pages=total_pages)


@ed_bp.route('/upload', methods=['POST'])
def upload():
    f = request.files.get('file')
    if not f or not f.filename.lower().endswith('.pdf'):
        return "Only PDF files are accepted.", 400
    pdf_bytes = f.read()
    name  = request.form.get('drawing_name', '').strip() or f.filename
    dtype = request.form.get('drawing_type', 'General')

    design_files = [
        (u.filename, u.read())
        for u in request.files.getlist('design_inputs')
        if u and u.filename
    ]

    # Parse design inputs once; store JSON so re-uploads can reuse without re-uploading files
    design_data, parse_errors = parse_design_inputs(design_files)
    design_data_json = json.dumps(design_data) if design_data else None

    drawing_id = str(uuid.uuid4())
    review_id  = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = _get_db()
    cur  = conn.cursor()
    cur.execute('INSERT INTO drawings (id, name, drawing_type, created_at) VALUES (%s,%s,%s,%s)',
                (drawing_id, name, dtype, now))
    cur.execute(
        'INSERT INTO reviews (id, drawing_id, version, pdf_content, status, created_at, design_data) VALUES (%s,%s,1,%s,%s,%s,%s)',
        (review_id, drawing_id, psycopg2.Binary(pdf_bytes), 'processing', now, design_data_json)
    )
    conn.commit()

    try:
        issues, detected_type = run_check(pdf_bytes, design_data)
        for err in parse_errors:
            issues.append({
                'category': 'Input', 'title': 'Design input parse error',
                'description': f'Could not parse design input: {err}',
                'suggestion': 'Check that the Excel file matches the CASAD E2E BBS format.',
                'severity': 'error', 'page_num': 1,
                'x': 5, 'y': 5, 'width': 30, 'height': 8,
            })
        _save_issues(review_id, issues, cur)
        cur.execute("UPDATE reviews SET status='complete' WHERE id=%s", (review_id,))
        cur.execute("UPDATE drawings SET drawing_type=%s WHERE id=%s", (detected_type, drawing_id))
    except Exception as e:
        _save_issues(review_id, [{
            'category': 'System', 'title': 'Checker error',
            'description': f'An unexpected error occurred: {e}',
            'suggestion': 'Check server logs.',
            'severity': 'error', 'page_num': 1,
            'x': 5, 'y': 5, 'width': 30, 'height': 8,
        }], cur)
        cur.execute("UPDATE reviews SET status='complete' WHERE id=%s", (review_id,))

    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('ed.review', review_id=review_id))


@ed_bp.route('/review/<review_id>')
def review(review_id):
    conn = _get_db()
    cur  = conn.cursor()
    cur.execute('''
        SELECT r.id, r.version, r.status, r.created_at,
               d.name, d.drawing_type, d.id AS drawing_id
        FROM reviews r JOIN drawings d ON d.id = r.drawing_id
        WHERE r.id = %s
    ''', (review_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return "Review not found", 404
    cur.execute('SELECT id, version FROM reviews WHERE drawing_id=%s ORDER BY version',
                (row['drawing_id'],))
    versions = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('review.html', review=dict(row), versions=[dict(v) for v in versions])


@ed_bp.route('/pdf/<review_id>')
def serve_pdf(review_id):
    conn = _get_db()
    cur  = conn.cursor()
    cur.execute('SELECT pdf_content FROM reviews WHERE id=%s', (review_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return "Not found", 404
    return Response(bytes(row['pdf_content']), mimetype='application/pdf',
                    headers={'Content-Disposition': 'inline'})


@ed_bp.route('/api/review/<review_id>/issues')
def api_issues(review_id):
    conn = _get_db()
    cur  = conn.cursor()
    cur.execute('SELECT * FROM issues WHERE review_id=%s ORDER BY severity DESC, category',
                (review_id,))
    issues = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(i) for i in issues])


@ed_bp.route('/api/review/<review_id>/extract')
def api_extract_debug(review_id):
    """Re-run Claude extraction on the stored PDF and return raw drawing_data JSON.
    Costs one Claude API call and takes ~30s. For debugging only."""
    conn = _get_db()
    cur  = conn.cursor()
    cur.execute('SELECT pdf_content FROM reviews WHERE id=%s', (review_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return jsonify({'error': 'Review not found'}), 404
    from ed_checker.pdf_extractor import extract_from_drawing
    data = extract_from_drawing(bytes(row['pdf_content']))
    data.pop('raw_text', None)  # omit verbose line list
    return jsonify(data)


@ed_bp.route('/api/issues/<issue_id>/status', methods=['POST'])
def update_status(issue_id):
    data = request.get_json()
    conn = _get_db()
    cur  = conn.cursor()
    cur.execute('UPDATE issues SET status=%s WHERE id=%s', (data.get('status', 'open'), issue_id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True})


@ed_bp.route('/api/diagnostics')
def diagnostics():
    """Quick environment check — visit /ed/api/diagnostics to verify setup."""
    info = {}

    # API key
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    info['anthropic_api_key'] = 'set' if key else 'MISSING'

    # PyMuPDF
    try:
        import fitz
        info['pymupdf'] = fitz.__version__
    except ImportError as e:
        info['pymupdf'] = f'MISSING — {e}'

    # pdfplumber
    try:
        import pdfplumber
        info['pdfplumber'] = pdfplumber.__version__
    except ImportError as e:
        info['pdfplumber'] = f'MISSING — {e}'

    # openpyxl
    try:
        import openpyxl
        info['openpyxl'] = openpyxl.__version__
    except ImportError as e:
        info['openpyxl'] = f'MISSING — {e}'

    # DB
    try:
        conn = _get_db()
        conn.close()
        info['database'] = 'connected'
    except Exception as e:
        info['database'] = f'ERROR — {e}'

    all_ok = all('MISSING' not in str(v) and 'ERROR' not in str(v) for v in info.values())
    info['status'] = 'ok' if all_ok else 'degraded'
    return jsonify(info)


@ed_bp.route('/reupload/<drawing_id>', methods=['GET', 'POST'])
def reupload(drawing_id):
    conn = _get_db()
    cur  = conn.cursor()
    cur.execute('SELECT * FROM drawings WHERE id=%s', (drawing_id,))
    drawing = cur.fetchone()
    if not drawing:
        cur.close(); conn.close()
        return "Drawing not found", 404

    if request.method == 'GET':
        cur.execute('SELECT MAX(version) AS v FROM reviews WHERE drawing_id=%s', (drawing_id,))
        last = cur.fetchone()
        cur.execute('''
            SELECT i.* FROM issues i
            JOIN reviews r ON r.id = i.review_id
            WHERE r.drawing_id=%s AND i.status='open'
              AND r.version=(SELECT MAX(version) FROM reviews WHERE drawing_id=%s)
            ORDER BY i.severity DESC
        ''', (drawing_id, drawing_id))
        open_issues = cur.fetchall()
        cur.close(); conn.close()
        return render_template('reupload.html',
                               drawing=dict(drawing),
                               current_version=last['v'],
                               open_issues=[dict(i) for i in open_issues])

    # POST — new version
    f = request.files.get('file')
    if not f or not f.filename.lower().endswith('.pdf'):
        cur.close(); conn.close()
        return "Only PDF files are accepted.", 400
    pdf_bytes = f.read()

    # Load design_data from the most recent previous version — no need to re-upload files
    cur.execute(
        'SELECT design_data FROM reviews WHERE drawing_id=%s ORDER BY version DESC LIMIT 1',
        (drawing_id,)
    )
    prev = cur.fetchone()
    design_data = json.loads(prev['design_data']) if prev and prev['design_data'] else {}

    # Allow overriding design inputs if new files are uploaded on this re-upload
    new_design_files = [
        (u.filename, u.read())
        for u in request.files.getlist('design_inputs')
        if u and u.filename
    ]
    parse_errors = []
    if new_design_files:
        design_data, parse_errors = parse_design_inputs(new_design_files)

    design_data_json = json.dumps(design_data) if design_data else None

    cur.execute('SELECT MAX(version) AS v FROM reviews WHERE drawing_id=%s', (drawing_id,))
    last = cur.fetchone()
    new_ver   = (last['v'] or 0) + 1
    review_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    cur.execute(
        'INSERT INTO reviews (id, drawing_id, version, pdf_content, status, created_at, design_data) VALUES (%s,%s,%s,%s,%s,%s,%s)',
        (review_id, drawing_id, new_ver, psycopg2.Binary(pdf_bytes), 'processing', now, design_data_json)
    )
    conn.commit()

    try:
        issues, detected_type = run_check(pdf_bytes, design_data)
        for err in parse_errors:
            issues.append({
                'category': 'Input', 'title': 'Design input parse error',
                'description': f'Could not parse design input: {err}',
                'suggestion': 'Check that the Excel file matches the CASAD E2E BBS format.',
                'severity': 'error', 'page_num': 1,
                'x': 5, 'y': 5, 'width': 30, 'height': 8,
            })
        _save_issues(review_id, issues, cur)
        cur.execute("UPDATE reviews SET status='complete' WHERE id=%s", (review_id,))
        cur.execute("UPDATE drawings SET drawing_type=%s WHERE id=%s", (detected_type, drawing_id))
    except Exception as e:
        _save_issues(review_id, [{
            'category': 'System', 'title': 'Checker error',
            'description': f'An unexpected error occurred: {e}',
            'suggestion': 'Check server logs.',
            'severity': 'error', 'page_num': 1,
            'x': 5, 'y': 5, 'width': 30, 'height': 8,
        }], cur)
        cur.execute("UPDATE reviews SET status='complete' WHERE id=%s", (review_id,))

    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('ed.review', review_id=review_id))
