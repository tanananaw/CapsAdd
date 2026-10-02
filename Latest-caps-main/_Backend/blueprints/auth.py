import secrets
import sqlite3
import traceback
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify
from werkzeug.security import check_password_hash, generate_password_hash

from database import get_db_connection
from extensions import oauth
from login_security import record_login_attempt, is_locked_out
from utils import log_activity, get_redirect_for_role, send_branded_email

auth_bp = Blueprint('auth', __name__)

# How long a password-reset OTP stays valid, and how soon another one can
# be issued for the same account - the cooldown stops a refreshed/resubmitted
# form (or someone spamming the form) from mail-bombing one inbox with a
# fresh code every time. Shorter than the old link-based lifetime (30 min)
# since a 6-digit code is meant to be typed in immediately after arriving,
# not saved for later, and a shorter window also narrows the brute-force
# surface described below.
RESET_TOKEN_LIFETIME_MINUTES = 10
RESET_REQUEST_COOLDOWN_MINUTES = 2

# A 6-digit code only has 1,000,000 possible values - far fewer than the
# old secrets.token_urlsafe(32) link, which was only ever guessable by
# brute force in a practical sense. Capping wrong guesses per issued code
# is what actually keeps this safe; without it, a script could just try
# all million combinations against a known username within the code's
# lifetime.
MAX_OTP_ATTEMPTS = 5


def _generate_otp_code():
    return ''.join(secrets.choice('0123456789') for _ in range(6))


def _send_password_reset_email(email, fullname, otp_code):
    try:
        send_branded_email(
            subject='MD57 Car Wash - Password Reset Code',
            recipient=email,
            heading='Password Reset Code',
            highlight=otp_code,
            plain_text_body=(
                f"Hello {fullname or 'there'}!\n\n"
                f"We received a request to reset your MD57 Car Wash account password.\n\n"
                f"Your password reset code is: {otp_code}\n\n"
                f"Enter this code on the password reset page to continue. It's valid for "
                f"{RESET_TOKEN_LIFETIME_MINUTES} minutes and can only be used once.\n\n"
                f"If you didn't request this, you can safely ignore this email - your password will not change.\n\n"
                f"- MD57 Team"
            ),
        )
    except Exception as e:
        print(f"[WARNING] Failed to send password reset email to {email}: {e}")


def _start_session(user):
    session.clear()
    session['user_id'] = user['user_id']
    session['user'] = user['username']
    session['role'] = user['role']
    session['fullname'] = user['fullname'] if user['fullname'] else user['username']


@auth_bp.route('/auth/google')
def google_login():
    redirect_uri = url_for('auth.google_auth_callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route('/auth/google/callback')
def google_auth_callback():
    try:
        token = oauth.google.authorize_access_token()
        user_info = token.get('userinfo')
        if not user_info:
            resp = oauth.google.get('userinfo')
            user_info = resp.json()

        email = user_info.get('email')
        fullname = user_info.get('name', email)

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM users WHERE username = ?', (email,))
        user = cursor.fetchone()

        if not user:
            # A random password that's immediately discarded after hashing -
            # nobody, including this code, ever knows it. Previously this
            # was a fixed string ('google_auth_secure_2026') sitting in
            # source control, which meant anyone who read this file could
            # log into ANY Google-authenticated account through the normal
            # /login form. auth_provider='google' below closes that off
            # entirely regardless: password-based login is now refused for
            # these accounts outright, not just hard to guess.
            unusable_password = generate_password_hash(secrets.token_hex(32))
            cursor.execute(
                'INSERT INTO users (username, password_hash, role, fullname, auth_provider) VALUES (?, ?, ?, ?, ?)',
                (email, unusable_password, 'customer', fullname, 'google')
            )
            conn.commit()
            cursor.execute('SELECT * FROM users WHERE username = ?', (email,))
            user = cursor.fetchone()

        conn.close()

        _start_session(user)
        log_activity(user['username'], f"Successfully logged in via Google as {user['role']}")
        return redirect(get_redirect_for_role(user['role']))

    except Exception as e:
        print(f"Google Auth Error: {e}")
        return redirect(url_for('auth.login_page', error='Google sign-in failed. Please try again.'))


@auth_bp.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if not username or not password:
            return render_template('login.html', error='Please fill in both username and password!')

        username = username.strip()
        ip_address = request.remote_addr

        locked, retry_minutes = is_locked_out(username, ip_address)
        if locked:
            log_activity(username, 'Login blocked: too many failed attempts')
            return render_template(
                'login.html',
                error=f'Too many failed login attempts. Please try again in {retry_minutes} minutes.'
            )

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
        user = cursor.fetchone()
        conn.close()

        if user and user['auth_provider'] == 'google':
            log_activity(username, 'Login blocked: account uses Google Sign-In, password login refused')
            return render_template(
                'login.html',
                error='This account uses Google Sign-In. Please use the "Sign in with Google" button instead.'
            )

        if user and check_password_hash(user['password_hash'], password):
            record_login_attempt(username, ip_address, True)
            _start_session(user)
            log_activity(user['username'], f"Successfully logged in as {user['role']}")

            next_url = request.args.get('next')
            if next_url and not next_url.startswith('//') and not next_url.startswith('http'):
                return redirect(next_url)
            return redirect(get_redirect_for_role(user['role']))

        record_login_attempt(username, ip_address, False)
        log_activity(username, 'Failed login attempt')
        return render_template('login.html', error='Invalid username or password!')

    return render_template('login.html')


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password_page():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()

        if username:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
            user = cursor.fetchone()

            if user and user['auth_provider'] == 'google':
                # Google-linked accounts have no real password to reset -
                # nothing to email a code for.
                log_activity(username, 'Password reset requested for a Google-linked account - no email sent')
            elif user:
                cutoff = (datetime.now() - timedelta(minutes=RESET_REQUEST_COOLDOWN_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')
                cursor.execute(
                    "SELECT 1 FROM password_resets WHERE user_id = ? AND used = 0 AND created_at > ?",
                    (user['user_id'], cutoff),
                )
                if cursor.fetchone():
                    log_activity(username, 'Password reset requested - throttled, a recent code is still valid')
                else:
                    now = datetime.now()
                    created_at = now.strftime('%Y-%m-%d %H:%M:%S')
                    expires_at = (now + timedelta(minutes=RESET_TOKEN_LIFETIME_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')
                    # created_at is set explicitly here (Python's local
                    # datetime.now()) rather than relying on the column's
                    # DEFAULT CURRENT_TIMESTAMP, which SQLite evaluates in
                    # UTC - the cooldown check above compares against a
                    # cutoff built from the same local datetime.now(), so
                    # both sides need the same clock or the throttle
                    # silently never matches (same class of bug as
                    # login_security.py's lockout - see ROADMAP.md item 4).
                    #
                    # `token` is still UNIQUE (a leftover from the old
                    # long-random-link design, where global uniqueness was
                    # free). A 6-digit OTP only has 1,000,000 possible
                    # values and password_resets rows are never deleted -
                    # given enough resets requested over this app's
                    # lifetime, two different accounts will eventually draw
                    # the same code and collide on that constraint. A
                    # crash here (uncaught IntegrityError -> raw 500) would
                    # also break the anti-enumeration property this whole
                    # flow is built around (a crash is a distinguishable
                    # response). Retrying with a fresh code on collision
                    # costs nothing and is honest about what "unique" here
                    # actually needs to mean (unique among currently-live
                    # codes, not for all time).
                    otp_code = None
                    for _attempt in range(5):
                        candidate = _generate_otp_code()
                        try:
                            cursor.execute(
                                "INSERT INTO password_resets (user_id, token, created_at, expires_at, attempts) VALUES (?, ?, ?, ?, 0)",
                                (user['user_id'], candidate, created_at, expires_at),
                            )
                            otp_code = candidate
                            break
                        except sqlite3.IntegrityError:
                            continue

                    if otp_code is None:
                        log_activity(username, 'Password reset requested - failed to generate a free OTP code after retries')
                    else:
                        conn.commit()
                        _send_password_reset_email(user['username'], user['fullname'], otp_code)
                        log_activity(username, 'Password reset requested - OTP emailed')

            conn.close()

        # Always render the same "enter your code" step regardless of
        # whether the account existed, was Google-linked, or the email
        # actually sent - anything that varied by those cases would let an
        # attacker enumerate real accounts by watching for a different
        # response. Verification of the code itself happens on submit.
        return render_template('reset_password.html', username=username)

    return render_template('forgot_password.html')


@auth_bp.route('/reset-password', methods=['GET', 'POST'])
def reset_password_page():
    if request.method == 'GET':
        # Nothing to verify without a code having been requested first -
        # there's no token in the URL anymore for a bookmarked/shared link
        # to carry.
        return redirect(url_for('auth.forgot_password_page'))

    username = (request.form.get('username') or '').strip()
    otp_code = (request.form.get('otp') or '').strip()
    new_password = request.form.get('password')
    confirm_password = request.form.get('confirm_password')

    generic_invalid = 'Invalid or expired code. Please check the code or request a new one.'

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
    user = cursor.fetchone()

    if not user:
        conn.close()
        return render_template('reset_password.html', username=username, error=generic_invalid)

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute(
        """
        SELECT reset_id, token, attempts FROM password_resets
        WHERE user_id = ? AND used = 0 AND expires_at > ?
        ORDER BY reset_id DESC LIMIT 1
        """,
        (user['user_id'], now_str),
    )
    reset_row = cursor.fetchone()

    if not reset_row:
        conn.close()
        return render_template('reset_password.html', username=username, error=generic_invalid)

    if reset_row['attempts'] >= MAX_OTP_ATTEMPTS:
        cursor.execute("UPDATE password_resets SET used = 1 WHERE reset_id = ?", (reset_row['reset_id'],))
        conn.commit()
        conn.close()
        log_activity(username, 'Password reset code locked out after too many incorrect attempts')
        return render_template(
            'reset_password.html', username=username,
            error='Too many incorrect attempts. Please request a new code.',
        )

    if reset_row['token'] != otp_code:
        cursor.execute("UPDATE password_resets SET attempts = attempts + 1 WHERE reset_id = ?", (reset_row['reset_id'],))
        conn.commit()
        conn.close()
        return render_template('reset_password.html', username=username, error=generic_invalid)

    if not new_password or len(new_password) < 6:
        conn.close()
        return render_template('reset_password.html', username=username, error='Password must be at least 6 characters.')
    if new_password != confirm_password:
        conn.close()
        return render_template('reset_password.html', username=username, error='Passwords do not match.')

    new_hash = generate_password_hash(new_password)
    cursor.execute("UPDATE users SET password_hash = ? WHERE user_id = ?", (new_hash, user['user_id']))
    # Invalidates every outstanding code for this account, not just the one
    # just used - an older, still-unused code shouldn't remain valid after
    # a successful reset.
    cursor.execute("UPDATE password_resets SET used = 1 WHERE user_id = ?", (user['user_id'],))
    conn.commit()
    conn.close()

    log_activity(username, 'Password reset completed')
    return redirect(url_for('auth.login_page', reset=1))


def _register_error(message, status=400):
    if request.is_json:
        return jsonify({'status': 'error', 'message': message}), status
    return render_template('register.html', error=message)


@auth_bp.route('/register', methods=['GET', 'POST'])
def register_page():
    if request.method == 'POST':
        try:
            data = request.get_json() if request.is_json else request.form
            email = (data.get('email') or '').strip()
            password = data.get('password')
            fullname = (data.get('fullname') or '').strip()

            if not email or not password:
                return _register_error('Email and password are required!')

            if len(password) < 6:
                return _register_error('Password must be at least 6 characters.')

            conn = get_db_connection()
            cursor = conn.cursor()

            cursor.execute('SELECT * FROM users WHERE username = ?', (email,))
            existing = cursor.fetchone()
            if existing:
                conn.close()
                return _register_error('Email is already registered! Please sign in.')

            hashed_pw = generate_password_hash(password)
            cursor.execute(
                'INSERT INTO users (username, password_hash, role, fullname) VALUES (?, ?, ?, ?)',
                (email, hashed_pw, 'customer', fullname if fullname else email)
            )
            conn.commit()
            conn.close()

            log_activity(email, 'Registered new customer account')
            if request.is_json:
                return jsonify({'status': 'success', 'message': 'Account registered successfully!'}), 200
            return redirect(url_for('auth.login_page', registered=1))

        except Exception as e:
            traceback.print_exc()
            return _register_error(f"Registration failed: {e}", 500)

    return render_template('register.html')


@auth_bp.route('/logout')
def logout():
    username = session.get('user', 'Unknown')
    log_activity(username, 'Logged out from system')
    session.clear()
    return redirect(url_for('auth.login_page'))
