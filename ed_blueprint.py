# ed_blueprint.py — ED Checker (Drawing Review) app mounted at /ed
import gc
import os
import gzip
import time
import uuid
import logging
import threading
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
    return psycopg2.connect(
        url,
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )


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
        ALTER TABLE reviews ADD COLUMN IF NOT EXISTS dxf_content BYTEA;
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
    _recover_stuck_reviews()


# Advisory lock key for the recovery sweep — arbitrary constant, must only be
# unique within this database. Prevents both gunicorn workers (each imports
# main.py and calls init_ed_db) from recovering the same reviews twice.
_RECOVERY_LOCK_KEY = 874512369


def _recover_stuck_reviews():
    """Re-run checks orphaned by a mid-check restart (deploy/OOM kills the
    daemon thread between the reviews INSERT and the status='complete' UPDATE,
    leaving the row 'processing' forever and the review page polling forever).
    Called at startup, when nothing else can legitimately be processing.
    Everything needed to re-run is stored in the row: pdf_content, dxf_content,
    design_data. Runs sequentially in one thread — parallel re-runs of large
    DXFs would recreate the memory spike that causes these orphans."""
    log = logging.getLogger('ed.recover')

    def _sweep():
        try:
            # Dedicated connection holds the advisory lock for the whole sweep;
            # the losing worker sees the lock taken and skips.
            lock_conn = _get_db()
            lock_cur  = lock_conn.cursor()
            lock_cur.execute('SELECT pg_try_advisory_lock(%s) AS got', (_RECOVERY_LOCK_KEY,))
            if not lock_cur.fetchone()['got']:
                lock_conn.close()
                return
            try:
                lock_cur.execute("SELECT id FROM reviews WHERE status='processing'")
                stuck_ids = [r['id'] for r in lock_cur.fetchall()]
                if not stuck_ids:
                    return
                log.info('Found %d stuck review(s): %s', len(stuck_ids), stuck_ids)
                for rid in stuck_ids:
                    # Fetch content per review so only one PDF+DXF is in memory at a time
                    conn = _get_db()
                    cur  = conn.cursor()
                    cur.execute('SELECT drawing_id, pdf_content, dxf_content, design_data '
                                'FROM reviews WHERE id=%s', (rid,))
                    row = cur.fetchone()
                    if not row:
                        cur.close(); conn.close()
                        continue
                    # Drop any issues a partially-completed run managed to insert
                    cur.execute('DELETE FROM issues WHERE review_id=%s', (rid,))
                    conn.commit()
                    cur.close()
                    conn.close()
                    log.info('Recovering stuck review %s — re-running check', rid)
                    recovery_errors = []
                    if not row['dxf_content']:
                        recovery_errors.append({
                            'category': 'System',
                            'title': 'DXF unavailable — PDF-only re-check',
                            'description': (
                                'The service restarted before the DXF could be saved. '
                                'This review was re-run in PDF-only mode. '
                                'Section presence checks (TABLE-1, SECTION C-C, etc.) '
                                'may show false positives.'
                            ),
                            'suggestion': 'Re-upload the drawing with the DXF to get accurate results.',
                            'severity': 'error', 'page_num': 1,
                            'x': 5, 'y': 5, 'width': 30, 'height': 8,
                        })
                    _run_check_bg(rid, row['drawing_id'], bytes(row['pdf_content']),
                                  json.loads(row['design_data']) if row['design_data'] else {},
                                  _decompress_dxf(row['dxf_content']), recovery_errors)
            finally:
                lock_cur.execute('SELECT pg_advisory_unlock(%s)', (_RECOVERY_LOCK_KEY,))
                lock_conn.commit()
                lock_conn.close()
        except Exception:
            log.exception('stuck-review recovery failed')

    threading.Thread(target=_sweep, daemon=True).start()


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


def _run_check_bg(review_id, drawing_id, pdf_bytes, design_data, dxf_bytes, parse_errors):
    """Run drawing check in a background thread; saves issues and marks review complete."""
    log = logging.getLogger('ed.bg')
    t0 = time.time()
    log.info('BG START review=%s pdf=%dKB dxf=%dKB', review_id,
             len(pdf_bytes)//1024, len(dxf_bytes)//1024 if dxf_bytes else 0)

    # Store DXF first so that if the service restarts mid-check, recovery
    # can re-run with the DXF instead of falling back to PDF-only mode.
    if dxf_bytes:
        try:
            _dxf_conn = _get_db()
            _dxf_cur  = _dxf_conn.cursor()
            log.info('BG DXF pre-store — %.1fs', time.time()-t0)
            _dxf_cur.execute("UPDATE reviews SET dxf_content=%s WHERE id=%s",
                             (psycopg2.Binary(_compress_dxf(dxf_bytes)), review_id))
            _dxf_conn.commit()
            _dxf_cur.close()
            _dxf_conn.close()
            log.info('BG DXF pre-store done — %.1fs', time.time()-t0)
        except Exception:
            log.warning('BG DXF pre-store failed — continuing anyway')

    try:
        issues, detected_type = run_check(pdf_bytes, design_data, dxf_bytes=dxf_bytes)
        log.info('BG run_check done — %.1fs, %d issues, type=%s', time.time()-t0, len(issues), detected_type)
        for err in parse_errors:
            issues.append({
                'category': 'Input', 'title': 'Design input parse error',
                'description': f'Could not parse design input: {err}',
                'suggestion': 'Check that the Excel file matches the CASAD E2E BBS format.',
                'severity': 'error', 'page_num': 1,
                'x': 5, 'y': 5, 'width': 30, 'height': 8,
            })
    except Exception as e:
        log.exception('BG run_check FAILED after %.1fs — review %s', time.time()-t0, review_id)
        issues = [{
            'category': 'System', 'title': 'Checker error',
            'description': f'An unexpected error occurred: {e}',
            'suggestion': 'Check server logs.',
            'severity': 'error', 'page_num': 1,
            'x': 5, 'y': 5, 'width': 30, 'height': 8,
        }]
        detected_type = 'General'

    # ezdxf inflates a 25 MB DXF into hundreds of MB of cyclic entity objects —
    # reclaim them before the DB write allocates the compressed-DXF copies.
    # Back-to-back checks without this tipped the 512 MB instance into OOM.
    gc.collect()

    try:
        conn = _get_db()
        cur  = conn.cursor()
        _save_issues(review_id, issues, cur)
        cur.execute("UPDATE reviews SET status='complete' WHERE id=%s", (review_id,))
        cur.execute("UPDATE drawings SET drawing_type=%s WHERE id=%s", (detected_type, drawing_id))
        conn.commit()
        cur.close()
        conn.close()
        log.info('BG COMPLETE — review %s, %d issues, total %.1fs', review_id, len(issues), time.time()-t0)
    except Exception as e:
        log.exception('BG DB save FAILED after %.1fs — review %s', time.time()-t0, review_id)


def _compress_dxf(b: bytes) -> bytes:
    return gzip.compress(b) if b else None


def _decompress_dxf(b) -> bytes:
    """Decompress gzip-compressed DXF. Falls back to raw bytes for legacy uncompressed rows."""
    if not b:
        return None
    raw = bytes(b)
    try:
        return gzip.decompress(raw)
    except OSError:
        return raw  # legacy row stored uncompressed


def _read_dxf_upload(file_storage) -> bytes:
    """Read an uploaded DXF file field. The browser gzips the DXF before upload
    (upload.js, CompressionStream) and sends it as <name>.dxf.gz — gunzip it here.
    Raw .dxf is still accepted (old browsers without CompressionStream)."""
    if not file_storage or not file_storage.filename:
        return None
    name = file_storage.filename.lower()
    if name.endswith('.dxf.gz'):
        try:
            return gzip.decompress(file_storage.read())
        except OSError:
            logging.getLogger('ed.upload').warning(
                'DXF upload %s: gzip decompress failed — ignoring DXF', file_storage.filename)
            return None
    if name.endswith('.dxf'):
        return file_storage.read()
    return None


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
    _ulog = logging.getLogger('ed.upload')
    t0 = time.time()
    _ulog.info('UPLOAD START — receiving multipart body')

    f = request.files.get('file')
    if not f or not f.filename.lower().endswith('.pdf'):
        return "Only PDF files are accepted.", 400
    pdf_bytes = f.read()
    _ulog.info('STEP 1 PDF read — %.1fs, size=%d KB', time.time()-t0, len(pdf_bytes)//1024)

    name  = request.form.get('drawing_name', '').strip() or f.filename
    dtype = request.form.get('drawing_type', 'General')

    dxf_bytes = _read_dxf_upload(request.files.get('dxf_file'))
    _ulog.info('STEP 2 DXF read — %.1fs, size=%d KB', time.time()-t0, len(dxf_bytes)//1024 if dxf_bytes else 0)

    design_files = [
        (u.filename, u.read())
        for u in request.files.getlist('design_inputs')
        if u and u.filename
    ]
    _ulog.info('STEP 3 design files read — %.1fs, count=%d', time.time()-t0, len(design_files))

    design_data, parse_errors = parse_design_inputs(design_files)
    _ulog.info('STEP 4 design inputs parsed — %.1fs', time.time()-t0)

    design_data_json = json.dumps(design_data) if design_data else None
    drawing_id = str(uuid.uuid4())
    review_id  = str(uuid.uuid4())
    now = datetime.now().isoformat()

    conn = _get_db()
    _ulog.info('STEP 5 DB connected — %.1fs', time.time()-t0)

    cur  = conn.cursor()
    cur.execute('INSERT INTO drawings (id, name, drawing_type, created_at) VALUES (%s,%s,%s,%s)',
                (drawing_id, name, dtype, now))
    _ulog.info('STEP 6 drawings INSERT done — %.1fs', time.time()-t0)

    # dxf_content stored by background thread — keep this INSERT small (PDF only)
    cur.execute(
        'INSERT INTO reviews (id, drawing_id, version, pdf_content, status, created_at, design_data) '
        'VALUES (%s,%s,1,%s,%s,%s,%s)',
        (review_id, drawing_id, psycopg2.Binary(pdf_bytes), 'processing', now, design_data_json)
    )
    _ulog.info('STEP 7 reviews INSERT done — %.1fs', time.time()-t0)

    conn.commit()
    cur.close()
    conn.close()
    _ulog.info('STEP 8 DB commit+close — %.1fs', time.time()-t0)

    threading.Thread(
        target=_run_check_bg,
        args=(review_id, drawing_id, pdf_bytes, design_data, dxf_bytes, parse_errors),
        daemon=True,
    ).start()
    _ulog.info('STEP 9 background thread started — redirecting after %.1fs total', time.time()-t0)

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


@ed_bp.route('/api/review/<review_id>/status')
def api_review_status(review_id):
    conn = _get_db()
    cur  = conn.cursor()
    cur.execute('SELECT status FROM reviews WHERE id=%s', (review_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    return jsonify({'status': row['status']})


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

    # ezdxf (DXF extraction)
    try:
        import ezdxf
        info['ezdxf'] = ezdxf.__version__
    except ImportError as e:
        info['ezdxf'] = f'MISSING — {e}'

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

    # Load design_data and dxf_content from the most recent previous version
    cur.execute(
        'SELECT design_data, dxf_content FROM reviews WHERE drawing_id=%s ORDER BY version DESC LIMIT 1',
        (drawing_id,)
    )
    prev = cur.fetchone()
    design_data = json.loads(prev['design_data']) if prev and prev['design_data'] else {}
    # Reuse stored DXF unless the engineer uploads a new one
    dxf_bytes = _decompress_dxf(prev['dxf_content']) if prev and prev.get('dxf_content') else None

    # Allow overriding design inputs if new files are uploaded on this re-upload
    new_design_files = [
        (u.filename, u.read())
        for u in request.files.getlist('design_inputs')
        if u and u.filename
    ]
    parse_errors = []
    if new_design_files:
        design_data, parse_errors = parse_design_inputs(new_design_files)

    # Allow overriding DXF if new one is uploaded
    new_dxf_bytes = _read_dxf_upload(request.files.get('dxf_file'))
    if new_dxf_bytes:
        dxf_bytes = new_dxf_bytes

    design_data_json = json.dumps(design_data) if design_data else None

    cur.execute('SELECT MAX(version) AS v FROM reviews WHERE drawing_id=%s', (drawing_id,))
    last = cur.fetchone()
    new_ver   = (last['v'] or 0) + 1
    review_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    # dxf_content stored by background thread to keep this INSERT small
    cur.execute(
        'INSERT INTO reviews (id, drawing_id, version, pdf_content, status, created_at, design_data) '
        'VALUES (%s,%s,%s,%s,%s,%s,%s)',
        (review_id, drawing_id, new_ver, psycopg2.Binary(pdf_bytes), 'processing', now, design_data_json)
    )
    conn.commit()
    cur.close()
    conn.close()

    # Run check in background — user sees the review page immediately
    threading.Thread(
        target=_run_check_bg,
        args=(review_id, drawing_id, pdf_bytes, design_data, dxf_bytes, parse_errors),
        daemon=True,
    ).start()

    return redirect(url_for('ed.review', review_id=review_id))
