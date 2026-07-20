# ed_blueprint.py — ED Checker (Drawing Review) app mounted at /ed
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
from ed_checker._memutil import trim_memory

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
        ALTER TABLE reviews ADD COLUMN IF NOT EXISTS recovery_attempts INTEGER NOT NULL DEFAULT 0;
        -- Batch upload: multiple drawings submitted together against one shared
        -- design file. NULL for a normal single-drawing upload/reupload — the
        -- review page only shows the batch navigator when batch_id is set.
        ALTER TABLE reviews ADD COLUMN IF NOT EXISTS batch_id TEXT;
        ALTER TABLE reviews ADD COLUMN IF NOT EXISTS batch_index INTEGER;
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
        -- Engineering Reasoning Reviewer ("AI Reasoning" tab) only — NULL for
        -- every other category. severity stays 'error' uniformly (see
        -- CLAUDE.md's no-warning-tier rule); confidence is the field that
        -- actually distinguishes these lower-certainty, open-ended findings
        -- for the engineer, kept out of the severity-driven Issues list.
        ALTER TABLE issues ADD COLUMN IF NOT EXISTS confidence TEXT;
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

# A review may be auto-recovered once. An OOM kill (SIGKILL from the OS) is
# invisible to _run_check_bg's own try/except — that only catches normal
# Python exceptions — so a review whose check gets OOM-killed never reaches
# status='complete' and looks identical to a mid-deploy-restart orphan to
# this sweep. Without a cap, every replacement worker's boot re-triggers the
# sweep, which retries the SAME dxf_content, which OOMs again, forever — a
# crash loop that eats server resources and blocks the review page forever
# (observed in production: a stuck 'processing' review triggering a repeat
# SIGKILL/DB-timeout cycle). If a review is still stuck after one recovery
# attempt, the OOM is very likely deterministic for that specific file
# (size/complexity), not transient concurrent load — give up and surface a
# clear error instead of retrying indefinitely.
_MAX_RECOVERY_ATTEMPTS = 1


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
                lock_cur.execute("SELECT id, recovery_attempts FROM reviews WHERE status='processing'")
                stuck = lock_cur.fetchall()
                if not stuck:
                    return
                log.info('Found %d stuck review(s): %s', len(stuck), [r['id'] for r in stuck])
                for stuck_row in stuck:
                    rid = stuck_row['id']
                    if stuck_row['recovery_attempts'] >= _MAX_RECOVERY_ATTEMPTS:
                        log.warning(
                            'Review %s still stuck after %d recovery attempt(s) — giving up '
                            'rather than retrying again (see _MAX_RECOVERY_ATTEMPTS comment)',
                            rid, stuck_row['recovery_attempts'])
                        fail_conn = _get_db()
                        fail_cur = fail_conn.cursor()
                        _save_issues(rid, [{
                            'category': 'System',
                            'title': 'Checker error — ran out of server memory',
                            'description': (
                                'This drawing could not be processed — the server ran out of '
                                'memory while parsing it, more than once. This usually means '
                                'the DXF is unusually large or complex for the available '
                                'server capacity, not a transient issue.'
                            ),
                            'suggestion': (
                                'Try re-uploading a simplified/smaller DXF (purge unused '
                                'blocks/layers in AutoCAD first), or contact support if the '
                                'file size looks normal.'
                            ),
                            'severity': 'error', 'page_num': 1,
                            'x': 5, 'y': 5, 'width': 30, 'height': 8,
                        }], fail_cur)
                        fail_cur.execute("UPDATE reviews SET status='complete' WHERE id=%s", (rid,))
                        fail_conn.commit()
                        fail_cur.close(); fail_conn.close()
                        continue
                    # Fetch content per review so only one PDF+DXF is in memory at a time
                    conn = _get_db()
                    cur  = conn.cursor()
                    cur.execute('SELECT drawing_id, pdf_content, dxf_content, design_data '
                                'FROM reviews WHERE id=%s', (rid,))
                    row = cur.fetchone()
                    if not row:
                        cur.close(); conn.close()
                        continue
                    # Drop any issues a partially-completed run managed to insert, and
                    # record this attempt BEFORE running the check — if the check itself
                    # gets OOM-killed, the incremented counter (already committed) is what
                    # lets the next sweep recognise this isn't a fresh orphan.
                    cur.execute('DELETE FROM issues WHERE review_id=%s', (rid,))
                    cur.execute('UPDATE reviews SET recovery_attempts = recovery_attempts + 1 WHERE id=%s', (rid,))
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
                severity, page_num, x, y, width, height, status, confidence)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',%s)''',
            (str(uuid.uuid4()), review_id,
             issue.get('category', 'General'),
             issue.get('title', 'Issue'),
             issue.get('description', ''),
             issue.get('suggestion', ''),
             issue.get('severity', 'error'),
             issue.get('page_num', 1),
             issue.get('x', 5), issue.get('y', 5),
             issue.get('width', 20), issue.get('height', 10),
             issue.get('confidence'))
        )


def _run_batch_bg(created, design_data, parse_errors):
    """Run _run_check_bg for each drawing in a batch upload, one at a time.

    Sequential by design — parallel ezdxf parses of multiple DXFs would recreate
    the OOM risk _run_check_bg's trim_memory() call exists to prevent (see
    ed_checker/_memutil.py). Each drawing still gets its own status='processing'
    -> 'complete' transition and shows up in the review UI as soon as it's done,
    the rest keep processing behind it in this same background thread.
    """
    log = logging.getLogger('ed.batch')
    log.info('BATCH START — %d drawing(s)', len(created))
    for review_id, drawing_id, pdf_bytes, dxf_bytes in created:
        _run_check_bg(review_id, drawing_id, pdf_bytes, design_data, dxf_bytes, parse_errors)
    log.info('BATCH COMPLETE — %d drawing(s)', len(created))


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
            log.exception('BG DXF pre-store failed for review %s — continuing anyway '
                          '(run_check still uses the in-memory dxf_bytes; only the DB '
                          'copy used by recovery/debug routes is affected)', review_id)

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
    # trim_memory() also releases the freed glibc arenas back to the OS — gc.collect()
    # alone leaves RSS at its high-water mark, see ed_checker/_memutil.py.
    trim_memory()

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
    Raw .dxf is still accepted (old browsers without CompressionStream).

    Every branch logs — including the "nothing received" case — so a batch-upload
    row whose DXF silently goes missing between the browser and here (as opposed
    to a genuine decompress failure) leaves a clear trace instead of just showing
    up later as an empty dxf_content in the DB with no record of why.
    """
    _dlog = logging.getLogger('ed.upload')
    if not file_storage or not file_storage.filename:
        _dlog.info('_read_dxf_upload: no file field present (file_storage=%r)', file_storage)
        return None
    name = file_storage.filename.lower()
    if name.endswith('.dxf.gz'):
        raw = file_storage.read()
        _dlog.info('_read_dxf_upload: received %r, %d compressed bytes', file_storage.filename, len(raw))
        if not raw:
            _dlog.warning('DXF upload %r: field present but 0 bytes received', file_storage.filename)
            return None
        try:
            out = gzip.decompress(raw)
            _dlog.info('_read_dxf_upload: decompressed %r to %d bytes', file_storage.filename, len(out))
            return out
        except OSError as e:
            _dlog.warning(
                'DXF upload %r: gzip decompress failed on %d received bytes (%s) — ignoring DXF',
                file_storage.filename, len(raw), e)
            return None
    if name.endswith('.dxf'):
        raw = file_storage.read()
        _dlog.info('_read_dxf_upload: received uncompressed %r, %d bytes', file_storage.filename, len(raw))
        return raw
    _dlog.warning('_read_dxf_upload: unrecognized filename %r — ignoring', file_storage.filename)
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


def _read_drawing_rows(request) -> list:
    """Collect drawing rows from a (possibly batch) upload form: file_0/dxf_file_0/
    drawing_name_0, file_1/dxf_file_1/drawing_name_1, ... — upload.js always emits
    this indexed shape, even for a single drawing (row 0), so there's one code path
    for both. Returns [{'name', 'pdf_bytes', 'dxf_bytes'}, ...]; stops at the first
    missing index so a gap can't silently truncate the batch.
    """
    _rlog = logging.getLogger('ed.upload')
    _rlog.info('_read_drawing_rows: request.files keys=%s', list(request.files.keys()))
    rows = []
    idx = 0
    while True:
        f = request.files.get(f'file_{idx}')
        if not f or not f.filename:
            break
        name = request.form.get(f'drawing_name_{idx}', '').strip() or f.filename
        dxf_field = request.files.get(f'dxf_file_{idx}')
        _rlog.info('_read_drawing_rows: row %d — dxf_file_%d present=%s filename=%r',
                   idx, idx, dxf_field is not None, getattr(dxf_field, 'filename', None))
        dxf_bytes = _read_dxf_upload(dxf_field)
        rows.append({'name': name, 'pdf_bytes': f.read(), 'dxf_bytes': dxf_bytes,
                     'filename': f.filename})
        idx += 1
    return rows


@ed_bp.route('/upload', methods=['POST'])
def upload():
    _ulog = logging.getLogger('ed.upload')
    t0 = time.time()
    _ulog.info('UPLOAD START — receiving multipart body')

    rows = _read_drawing_rows(request)
    if not rows:
        return "Only PDF files are accepted.", 400
    for i, row in enumerate(rows):
        if not row['filename'].lower().endswith('.pdf'):
            return f"Drawing {i + 1}: only PDF files are accepted.", 400
    _ulog.info('STEP 1 %d drawing row(s) read — %.1fs, total_pdf=%dKB total_dxf=%dKB',
               len(rows), time.time()-t0,
               sum(len(r['pdf_bytes']) for r in rows)//1024,
               sum(len(r['dxf_bytes'] or b'') for r in rows)//1024)

    dtype = request.form.get('drawing_type', 'General')

    design_files = [
        (u.filename, u.read())
        for u in request.files.getlist('design_inputs')
        if u and u.filename
    ]
    _ulog.info('STEP 2 design files read — %.1fs, count=%d', time.time()-t0, len(design_files))

    design_data, parse_errors = parse_design_inputs(design_files)
    _ulog.info('STEP 3 design inputs parsed — %.1fs', time.time()-t0)

    design_data_json = json.dumps(design_data) if design_data else None
    # batch_id only when there's actually more than one drawing — keeps a normal
    # single-drawing upload's DB rows identical to before this feature existed.
    batch_id = str(uuid.uuid4()) if len(rows) > 1 else None
    now = datetime.now().isoformat()

    conn = _get_db()
    _ulog.info('STEP 4 DB connected — %.1fs', time.time()-t0)
    cur = conn.cursor()

    created = []  # (review_id, drawing_id, pdf_bytes, dxf_bytes) — for the bg thread
    for i, row in enumerate(rows):
        drawing_id = str(uuid.uuid4())
        review_id  = str(uuid.uuid4())
        cur.execute('INSERT INTO drawings (id, name, drawing_type, created_at) VALUES (%s,%s,%s,%s)',
                    (drawing_id, row['name'], dtype, now))
        # dxf_content stored by the background thread — keep this INSERT small (PDF only)
        cur.execute(
            'INSERT INTO reviews (id, drawing_id, version, pdf_content, status, created_at, '
            'design_data, batch_id, batch_index) VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s)',
            (review_id, drawing_id, psycopg2.Binary(row['pdf_bytes']), 'processing', now,
             design_data_json, batch_id, i + 1)
        )
        created.append((review_id, drawing_id, row['pdf_bytes'], row['dxf_bytes']))
    _ulog.info('STEP 5 drawings+reviews INSERT done for %d row(s) — %.1fs', len(rows), time.time()-t0)

    conn.commit()
    cur.close()
    conn.close()
    _ulog.info('STEP 6 DB commit+close — %.1fs', time.time()-t0)

    # One thread, all rows processed sequentially — see _run_batch_bg docstring.
    threading.Thread(
        target=_run_batch_bg,
        args=(created, design_data, parse_errors),
        daemon=True,
    ).start()
    _ulog.info('STEP 7 background thread started — redirecting after %.1fs total', time.time()-t0)

    return redirect(url_for('ed.review', review_id=created[0][0]))


@ed_bp.route('/review/<review_id>')
def review(review_id):
    conn = _get_db()
    cur  = conn.cursor()
    cur.execute('''
        SELECT r.id, r.version, r.status, r.created_at, r.batch_id,
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

    # Batch navigator: "Drawing 2/4" with prev/next — only when this review was
    # created as part of a multi-drawing batch upload (see _read_drawing_rows).
    batch_nav = None
    if row['batch_id']:
        cur.execute('''
            SELECT r.id AS review_id
            FROM reviews r
            WHERE r.batch_id = %s
            ORDER BY r.batch_index
        ''', (row['batch_id'],))
        siblings = [s['review_id'] for s in cur.fetchall()]
        if len(siblings) > 1:
            idx = siblings.index(review_id)
            batch_nav = {
                'index':   idx + 1,
                'total':   len(siblings),
                'prev_id': siblings[idx - 1] if idx > 0 else None,
                'next_id': siblings[idx + 1] if idx < len(siblings) - 1 else None,
            }

    cur.close()
    conn.close()
    return render_template('review.html', review=dict(row), versions=[dict(v) for v in versions],
                           batch_nav=batch_nav)


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


@ed_bp.route('/api/review/<review_id>/dxf-schedule-debug')
def api_dxf_schedule_debug(review_id):
    """
    Re-run DXF schedule extraction on the stored DXF and return a detailed breakdown
    per bar mark: every raw text cell the extractor saw for each column, plus the
    parsed final values.  URL param ?bars=y,y1 limits output to specific marks.
    No Claude API call needed — pure DXF parsing.
    """
    conn = _get_db()
    cur  = conn.cursor()
    cur.execute('SELECT dxf_content FROM reviews WHERE id=%s', (review_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row or not row['dxf_content']:
        return jsonify({'error': 'No DXF stored for this review'}), 404

    dxf_bytes = _decompress_dxf(row['dxf_content'])
    filter_marks = {m.strip() for m in request.args.get('bars', '').split(',') if m.strip()}

    import ezdxf, tempfile, os as _os
    from ed_checker.dxf_extractor import (
        _collect_text, _get_extents, _units_to_mm,
        _group_rows, _build_col_map, _build_comp_boundaries,
        _is_bar_mark_token, PPP_PROFILE,
    )

    profile = PPP_PROFILE
    layout  = profile.layout
    diags   = []

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
            tmp.write(dxf_bytes)
            tmp_path = tmp.name
        try:
            doc = ezdxf.readfile(tmp_path)
        except Exception:
            from ezdxf import recover
            doc, _ = recover.readfile(tmp_path)
    finally:
        if tmp_path:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass

    msp = doc.modelspace()
    all_text, stats = _collect_text(msp)
    extents = _get_extents(doc, all_text, diags)
    u2mm    = _units_to_mm(doc, diags)

    x_min, y_min, x_max, y_max = extents
    dw = x_max - x_min
    dh = y_max - y_min
    sched_x_min = x_min + dw * layout.schedule_x_min_frac
    sched_text  = [t for t in all_text if t['x'] >= sched_x_min and not t.get('from_block')]

    rows = _group_rows(sched_text, tol_frac=layout.sched_row_tol_frac, extents=extents)

    # Find column header row (same logic as _extract_schedule)
    header_idx = None
    for idx, row in enumerate(rows):
        row_text = ' '.join(t['text'] for t in row).upper()
        if sum(1 for k in ('DIA', 'NOS', 'LENGTH') if k in row_text) >= 2:
            header_idx = idx
            break

    if header_idx is None:
        return jsonify({'error': 'Schedule column header row not found in DXF'}), 422

    header_y = rows[header_idx][0]['y']
    header_sub_rows = [row for row in rows if abs(row[0]['y'] - header_y) < dh * layout.header_band_frac]
    col_map = _build_col_map(header_sub_rows)

    if not col_map:
        return jsonify({'error': 'Column map empty — no known header keywords matched'}), 422

    data_rows   = rows[header_idx + 1:]
    comp_bounds = _build_comp_boundaries(data_rows, profile)
    comp_by_idx = {}
    for idx, comp in comp_bounds:
        comp_by_idx[idx] = comp

    # Build a simple idx→comp lookup covering data_rows
    current_comp = 'unknown'
    row_comp = {}
    for i, row in enumerate(data_rows):
        if i in comp_by_idx:
            current_comp = comp_by_idx[i]
        row_comp[i] = current_comp

    # Identify bar mark rows and their row indices within data_rows
    bm_row_idxs = {}  # bar_mark → list of row indices where that mark appears
    for i, row in enumerate(data_rows):
        for cell in row:
            s = cell['text'].strip().strip("'\"").lower()
            if _is_bar_mark_token(s):
                bm_row_idxs.setdefault(s, []).append(i)

    # For each bar mark, collect all rows in its range and dump raw cells per column
    # real col_map positions (no scratch keys)
    real_col_map = {k: v for k, v in col_map.items() if not k.endswith('_assigned_x')}
    x_span = (max(real_col_map.values()) - min(real_col_map.values())) if len(real_col_map) > 1 else 100.0
    x_tol  = x_span * 0.20

    col_keys_ordered = ['bar_mark', 'reinforcement', 'bar_dia_mm', 'spacing_mm',
                        'count', 'length_m', 'total_length_m', 'unit_wt_kg_m', 'total_wt_kg']

    result = {}
    bm_row_list = sorted(bm_row_idxs.items(), key=lambda kv: min(kv[1]))
    for idx_in_list, (bm, mark_rows) in enumerate(bm_row_list):
        if filter_marks and bm not in filter_marks:
            continue

        # Determine row range: from first mark row to just before next mark
        first_row  = min(mark_rows)
        next_marks = [r for (_, rs) in bm_row_list[idx_in_list+1:] for r in rs]
        last_row   = (min(next_marks) - 1) if next_marks else len(data_rows) - 1

        bar_rows = data_rows[first_row:last_row + 1]
        comp     = row_comp.get(first_row, 'unknown')

        # Gather all text items in range and assign to nearest column
        raw_by_col = {k: [] for k in col_keys_ordered}
        raw_by_col['_unassigned'] = []
        for row in bar_rows:
            for cell in row:
                nearest = min(real_col_map.items(), key=lambda kv: abs(kv[1] - cell['x']))
                field, fx = nearest
                entry = {'text': cell['text'], 'x': round(cell['x'], 1),
                         'y': round(cell['y'], 1)}
                if abs(fx - cell['x']) < x_tol:
                    raw_by_col.setdefault(field, []).append(entry)
                else:
                    raw_by_col['_unassigned'].append(entry)

        # Get final parsed values (re-run via the zone-aware orchestrator — splits
        # bar_rows into per-confinement-zone dicts when more than one TOTAL_LEN
        # anchor is present, e.g. y/y1; single-zone bars are unaffected)
        from ed_checker.dxf_extractor import _aggregate_bar_rows_for_bar
        parsed = _aggregate_bar_rows_for_bar(bar_rows, col_map, bm,
                                             x_offset_min=1000.0 / u2mm)
        n_zones = len(parsed) if isinstance(parsed, list) else 1
        result[bm] = {
            'component':   comp,
            'row_range':   [first_row, last_row],
            'n_zones':     n_zones,
            'raw_rows_text': [
                ' | '.join(c['text'] for c in sorted(row, key=lambda t: t['x']))
                for row in bar_rows
            ],
            'cells_by_col': {k: v for k, v in raw_by_col.items() if v},
            'col_map':     {k: round(v, 1) for k, v in real_col_map.items()},
            'parsed':      parsed,
        }

    return jsonify({
        'review_id':       review_id,
        'extents':         list(extents),
        'sched_x_min':     round(sched_x_min, 1),
        'col_map':         {k: round(v, 1) for k, v in real_col_map.items()},
        'comp_boundaries': [[i, c] for i, c in comp_bounds],
        'bars':            result,
    })


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
