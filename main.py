# main.py — Combined CASAD service entry point
# Runs both Bridge Inspection (WhatsApp bot) and ED Checker (web UI)
# on a single Render service.
#
# Routes:
#   /webhook, /health, /dashboard, /download/*  → Bridge Inspection (unchanged)
#   /ed/*                                        → ED Checker (drawing review)

import os
from flask import send_from_directory
from server import app              # Bridge Inspection app — all routes intact
from ed_blueprint import ed_bp, init_ed_db

# Mount ED Checker at /ed — zero conflict with bridge routes
app.register_blueprint(ed_bp, url_prefix='/ed')

# Explicit static file serving for ED Checker (more reliable than Blueprint static)
_ED_STATIC = os.path.join(os.path.dirname(__file__), 'ed_static')

@app.route('/ed/static/<path:filename>')
def ed_static(filename):
    return send_from_directory(_ED_STATIC, filename)

# Initialise ED Checker tables (bridge tables already init'd inside server.py)
with app.app_context():
    init_ed_db()

# Gunicorn uses this `app` object directly.
# For local dev: python main.py
if __name__ == '__main__':
    app.run(debug=True, port=5000)
