import os
from datetime import timedelta

from flask import Flask, jsonify, render_template, request, session
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from config import template_dir, static_dir
from database import get_db_connection, init_db  # get_db_connection re-exported: 18 test files import it via `from app import app, get_db_connection`
from extensions import init_extensions
from live_updates import is_announced_write, publish

app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)

# Behind a reverse-proxy tunnel (serveo, ngrok, Railway, etc.) HTTPS is
# terminated at the edge and this process only ever sees plain HTTP -
# without this, url_for(..., _external=True) (used to build the Google
# OAuth redirect URI) generates an http:// URL even though the public
# address is https://, which Google rejects as a redirect_uri mismatch
# despite the host/path being correct. Trusts one upstream proxy hop's
# X-Forwarded-Proto/Host headers.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Essential for Flask Session and RBAC Security
# NOTE: No hardcoded fallback. If SECRET_KEY is missing from .env, fail loudly
# instead of silently running with a secret that's sitting in source control.
app.secret_key = os.environ.get('SECRET_KEY')
if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY is not set. Add it to your .env file before starting the app."
    )

# Google OAuth Configuration
app.config['GOOGLE_CLIENT_ID'] = os.environ.get('GOOGLE_CLIENT_ID')
app.config['GOOGLE_CLIENT_SECRET'] = os.environ.get('GOOGLE_CLIENT_SECRET')
if not app.config['GOOGLE_CLIENT_ID'] or not app.config['GOOGLE_CLIENT_SECRET']:
    raise RuntimeError(
        "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set. Add them to your .env file."
    )

# --- SESSION TIMEOUT CONFIGURATION ---
# Loosened from the original 15-minute inactivity timeout to 12 hours on
# 2026-09-19 for local demo/dev convenience (user request - stepping away
# and coming back later shouldn't force a re-login). This was a deliberate
# security control; set SESSION_LIFETIME back to timedelta(minutes=15)
# before any real deployment.
SESSION_LIFETIME = timedelta(hours=12)
app.config['PERMANENT_SESSION_LIFETIME'] = SESSION_LIFETIME

# --- SESSION COOKIE HARDENING ---
# HTTPONLY is Flask's default already, but set explicitly so it's not
# silently relying on a default someone could change without noticing.
# SAMESITE=Lax works over plain HTTP too, so it's safe to always enable -
# it's defense-in-depth alongside CSRFProtect, not a replacement for it.
# SECURE (cookie only sent over HTTPS) defaults OFF because local/dev runs
# over plain HTTP and Secure would silently break login there - flip
# SESSION_COOKIE_SECURE=true in .env once this is deployed behind TLS.
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', 'False').lower() in ('true', '1')

# --- EMAIL CONFIGURATION (Flask-Mail) ---
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'True').lower() in ('true', '1')
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME'])
if not app.config['MAIL_USERNAME'] or not app.config['MAIL_PASSWORD']:
    print("[WARNING] MAIL_USERNAME/MAIL_PASSWORD not set - email notifications will fail.")

init_extensions(app)


def _wants_json():
    # /api/ callers expect JSON, not a rendered page.
    return request.path.startswith('/api/') or request.is_json


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    if _wants_json():
        return jsonify({'status': 'error', 'message': f'CSRF token validation failed: {e.description}'}), 400
    return render_template('login.html', error='Security validation failed or session expired. Please log in again.'), 400


# Branded 404/500 pages instead of Flask's bare default error output.
@app.errorhandler(404)
def handle_not_found(e):
    if _wants_json():
        return jsonify({'status': 'error', 'message': 'Not found.'}), 404
    return render_template(
        'error.html',
        error_code=404,
        error_title='Page Not Found',
        error_message="The page you're looking for doesn't exist or may have moved.",
    ), 404


# Also what every unhandled exception in a route ends up as - Flask logs the
# traceback first, and API callers get a generic message rather than the
# raw exception text.
@app.errorhandler(500)
def handle_server_error(e):
    if _wants_json():
        return jsonify({'status': 'error', 'message': 'Internal server error.'}), 500
    return render_template(
        'error.html',
        error_code=500,
        error_title='Something Went Wrong',
        error_message="An unexpected error occurred on our end. Please try again, or contact support if it keeps happening.",
    ), 500


# Any other HTTP error (unreadable JSON body, wrong method, ...) - same JSON
# shape for API callers instead of Werkzeug's default HTML page.
@app.errorhandler(HTTPException)
def handle_http_error(e):
    if _wants_json():
        return jsonify({'status': 'error', 'message': e.description}), e.code
    return e


@app.before_request
def make_session_permanent():
    session.permanent = True


# Any successful write through the API may change what another open page
# shows - tell live pages to refresh (see live_updates.py).
@app.after_request
def announce_changes(response):
    if response.status_code < 400 and is_announced_write(request.method, request.path):
        publish('data')
    return response


# --- ANTI-BROWSER CACHING ---
@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


init_db()

from blueprints.auth import auth_bp
from blueprints.pages import pages_bp
from blueprints.admin_api import admin_api_bp
from blueprints.queue_api import queue_api_bp
from blueprints.feedback_api import feedback_api_bp
from blueprints.staff_api import staff_api_bp
from blueprints.notifications_api import notifications_api_bp

app.register_blueprint(auth_bp)
app.register_blueprint(pages_bp)
app.register_blueprint(admin_api_bp)
app.register_blueprint(queue_api_bp)
app.register_blueprint(feedback_api_bp)
app.register_blueprint(staff_api_bp)
app.register_blueprint(notifications_api_bp)


if __name__ == '__main__':
    from no_show import start_no_show_sweeper
    from ai_retrain import start_ai_retrain_scheduler
    from shift_reminder import start_shift_reminder_scheduler, start_roster_watch
    from cleanup import start_cleanup_sweeper
    start_no_show_sweeper(app)
    start_ai_retrain_scheduler(app)
    start_shift_reminder_scheduler(app)
    start_roster_watch(app)
    start_cleanup_sweeper(app)
    app.run(host='0.0.0.0', port=5000, debug=False)
