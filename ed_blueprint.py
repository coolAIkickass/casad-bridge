# ed_blueprint.py — ED Checker (Drawing Review) app mounted at /ed
import os
import uuid
import psycopg2
import psycopg2.extras
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, Response

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


DUMMY_ISSUES = [
    {
        "category": "Title Block",
        "title": "Missing Revision Number",
        "description": "The title block does not contain a revision number. All drawings must carry a revision entry (R0, R1 …) before submission.",
        "suggestion": "Add revision number 'R0' in the Revision field of the title block.",
        "severity": "error", "page_num": 1,
        "x": 63, "y": 80, "width": 32, "height": 6,
    },
    {
        "category": "Title Block",
        "title": "Drawing Scale Not Specified",
        "description": "The scale field in the title block is empty. Drawings without an explicit scale are non-compliant with CASAD standards.",
        "suggestion": "Specify the drawing scale (e.g. 1:50) in the title block scale field.",
        "severity": "error", "page_num": 1,
        "x": 63, "y": 87, "width": 16, "height": 5,
    },
    {
        "category": "Dimensions",
        "title": "Pile Cap Depth Not Dimensioned",
        "description": "Overall pile cap length × width are shown but depth is missing from the section view.",
        "suggestion": "Add the pile cap depth dimension in the section view. Refer IRC:78 Cl. 709 for minimum thickness.",
        "severity": "error", "page_num": 1,
        "x": 15, "y": 25, "width": 30, "height": 22,
    },
    {
        "category": "Dimensions",
        "title": "Pedestal Height Not Dimensioned",
        "description": "Pedestal height above pile cap is not shown in the elevation view.",
        "suggestion": "Add vertical dimension: top of pile cap to top of pedestal.",
        "severity": "error", "page_num": 1,
        "x": 46, "y": 20, "width": 10, "height": 35,
    },
    {
        "category": "Reinforcement",
        "title": "Pile Bar Diameter Not Called Out",
        "description": "Longitudinal bars are shown but bar diameter and count are not annotated. IRC:78 requires full reinforcement specification.",
        "suggestion": "Add callout in format: N-TφD@S (e.g. 12-T16@200) per IRC:78 notation.",
        "severity": "error", "page_num": 1,
        "x": 62, "y": 35, "width": 22, "height": 18,
    },
    {
        "category": "Reinforcement",
        "title": "Stirrup Spacing Not Specified",
        "description": "Pile cap stirrups are drawn but spacing is not dimensioned or noted anywhere.",
        "suggestion": "Specify stirrup spacing in the section detail or add a note referencing the reinforcement schedule.",
        "severity": "warning", "page_num": 1,
        "x": 18, "y": 52, "width": 25, "height": 14,
    },
    {
        "category": "Cross-References",
        "title": "Section Mark Missing in Plan View",
        "description": "Section A-A is detailed but the cutting plane line and reference mark are absent from the plan view.",
        "suggestion": "Add section cut line A-A with direction arrows in the plan view, referencing the detail sheet number.",
        "severity": "warning", "page_num": 1,
        "x": 5, "y": 10, "width": 55, "height": 68,
    },
    {
        "category": "Notes",
        "title": "Concrete Grade Not Specified",
        "description": "General notes do not state the concrete grade for pile cap and piles. IRC:78 requires this to be explicit.",
        "suggestion": "Add note: 'Concrete for piles and pile cap: M35 as per IS:456.' Confirm grade with design engineer.",
        "severity": "warning", "page_num": 1,
        "x": 5, "y": 68, "width": 28, "height": 8,
    },
]


def _seed_issues(review_id, conn):
    cur = conn.cursor()
    for issue in DUMMY_ISSUES:
        cur.execute(
            '''INSERT INTO issues
               (id, review_id, category, title, description, suggestion,
                severity, page_num, x, y, width, height, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open')''',
            (str(uuid.uuid4()), review_id, issue['category'], issue['title'],
             issue['description'], issue['suggestion'], issue['severity'],
             issue['page_num'], issue['x'], issue['y'], issue['width'], issue['height'])
        )
    cur.close()


@ed_bp.route('/')
def index():
    conn = _get_db()
    cur = conn.cursor()
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
        LIMIT 20
    ''')
    recent = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('upload.html', recent=recent)


@ed_bp.route('/upload', methods=['POST'])
def upload():
    f = request.files.get('file')
    if not f or not f.filename.lower().endswith('.pdf'):
        return "Only PDF files are accepted.", 400
    pdf_bytes = f.read()
    name  = request.form.get('drawing_name', '').strip() or f.filename
    dtype = request.form.get('drawing_type', 'General')
    drawing_id = str(uuid.uuid4())
    review_id  = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = _get_db()
    cur  = conn.cursor()
    cur.execute('INSERT INTO drawings (id, name, drawing_type, created_at) VALUES (%s,%s,%s,%s)',
                (drawing_id, name, dtype, now))
    cur.execute(
        'INSERT INTO reviews (id, drawing_id, version, pdf_content, status, created_at) VALUES (%s,%s,1,%s,%s,%s)',
        (review_id, drawing_id, psycopg2.Binary(pdf_bytes), 'complete', now)
    )
    _seed_issues(review_id, conn)
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
    cur.execute('SELECT MAX(version) AS v FROM reviews WHERE drawing_id=%s', (drawing_id,))
    last = cur.fetchone()
    new_ver   = (last['v'] or 0) + 1
    review_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    cur.execute(
        'INSERT INTO reviews (id, drawing_id, version, pdf_content, status, created_at) VALUES (%s,%s,%s,%s,%s,%s)',
        (review_id, drawing_id, new_ver, psycopg2.Binary(pdf_bytes), 'complete', now)
    )
    _seed_issues(review_id, conn)
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('ed.review', review_id=review_id))
