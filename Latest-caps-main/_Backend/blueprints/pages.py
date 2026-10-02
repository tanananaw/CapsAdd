from flask import Blueprint, render_template, redirect, url_for, session

from utils import role_required

pages_bp = Blueprint('pages', __name__)


@pages_bp.context_processor
def inject_role():
    # The only per-user value these templates read directly - base.html
    # takes the rest (name, user id) straight from the session.
    return {'role': session.get('role')}


@pages_bp.route('/')
def home():
    if 'user' not in session:
        return redirect(url_for('auth.login_page'))
    return render_template('homepage.html')


@pages_bp.route('/queue')
@role_required('admin', 'staff', 'customer')
def queue_page():
    return render_template('queue_dashboard.html')


@pages_bp.route('/cancelled')
@role_required('admin', 'staff')
def cancelled_page():
    return render_template('cancelled.html')


@pages_bp.route('/ready')
@role_required('admin', 'staff')
def ready_page():
    return render_template('ready.html')


@pages_bp.route('/book')
@role_required('admin', 'staff', 'customer')
def booking_page():
    return render_template('booking.html')


@pages_bp.route('/my-bookings')
@role_required('customer')
def my_bookings_page():
    return render_template('my_bookings.html')


@pages_bp.route('/admin')
@role_required('admin')
def admin_dashboard():
    return render_template('admin-dashboard.html')


@pages_bp.route('/analytics')
@role_required('admin')
def analytics_page():
    # weather_api_key/weather_city used to be passed here so the browser
    # could call OpenWeatherMap directly - that leaked the API key into
    # the page's own HTML. The weather widget now calls
    # /api/weather-current, which makes that request server-side instead.
    return render_template('manager_analytics.html')


@pages_bp.route('/feedback')
@role_required('admin', 'staff', 'customer')
def feedback_page():
    return render_template('feedback.html')


@pages_bp.route('/history')
@role_required('admin', 'staff')
def history_page():
    return render_template('history.html')


@pages_bp.route('/staff')
@role_required('admin', 'staff')
def staff_page():
    return render_template('staff.html')


@pages_bp.route('/admin/create-user-page')
@role_required('admin')
def create_user_page():
    return render_template('create_user.html')


@pages_bp.route('/admin/manage-pricing')
@role_required('admin')
def manage_pricing_page():
    return render_template('manage_pricing.html')


@pages_bp.route('/privacy')
def privacy_page():
    # Deliberately no @role_required - this needs to be reachable by
    # anyone without logging in (Google's OAuth consent screen requires a
    # publicly-accessible privacy policy URL to move the app out of
    # Testing mode).
    return render_template('privacy.html')