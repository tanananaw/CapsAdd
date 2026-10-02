// MD57 Car Wash - shared page behavior (toast, logout, CSRF, theme toggle).
// Loaded on every authenticated page via base.html. Depends on the
// <meta name="csrf-token"> tag and the #floating-toast markup that
// base.html also provides.

(function() {
    // Attach the CSRF token to every non-GET fetch automatically, so page
    // scripts never have to remember to do it themselves.
    const originalFetch = window.fetch;
    window.fetch = function(url, options) {
        options = options || {};
        const method = (options.method || 'GET').toUpperCase();
        if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
            const token = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            if (token) {
                if (options.headers instanceof Headers) {
                    if (!options.headers.has('X-CSRFToken')) options.headers.append('X-CSRFToken', token);
                } else if (Array.isArray(options.headers)) {
                    options.headers.push(['X-CSRFToken', token]);
                } else {
                    options.headers = options.headers || {};
                    if (!options.headers['X-CSRFToken']) options.headers['X-CSRFToken'] = token;
                }
            }
        }
        return originalFetch(url, options);
    };
})();

function toggleTheme() {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    const next = isDark ? 'light' : 'dark';
    if (next === 'dark') {
        document.documentElement.setAttribute('data-theme', 'dark');
    } else {
        document.documentElement.removeAttribute('data-theme');
    }
    try { localStorage.setItem('md57_theme', next); } catch (e) {}
    syncThemeToggleIcon();
    // Chart.js draws its text straight onto <canvas> pixels, so it can't
    // react to the CSS variable swap on its own - pages with charts listen
    // for this to redraw with theme-appropriate colors.
    document.dispatchEvent(new CustomEvent('md57-theme-changed', { detail: { theme: next } }));
}

function syncThemeToggleIcon() {
    const icon = document.getElementById('themeToggleIcon');
    if (!icon) return;
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    icon.className = isDark ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
}

// Mobile off-canvas sidebar drawer (see the max-width:768px rules in
// style.css) - on desktop .sidebar-toggle-btn is display:none, so these
// never fire there; only the mobile drawer reads .sidebar's 'open' class.
function toggleSidebar() {
    const sidebar = document.getElementById('primarySidebar');
    if (!sidebar) return;
    if (sidebar.classList.contains('open')) {
        closeSidebar();
    } else {
        openSidebar();
    }
}

function openSidebar() {
    const sidebar = document.getElementById('primarySidebar');
    const backdrop = document.getElementById('sidebarBackdrop');
    const btn = document.getElementById('sidebarToggleBtn');
    const icon = document.getElementById('sidebarToggleIcon');
    if (!sidebar) return;
    sidebar.classList.add('open');
    if (backdrop) backdrop.classList.add('show');
    if (btn) btn.setAttribute('aria-expanded', 'true');
    if (icon) icon.className = 'fa-solid fa-xmark';
    document.body.style.overflow = 'hidden';
}

function closeSidebar() {
    const sidebar = document.getElementById('primarySidebar');
    const backdrop = document.getElementById('sidebarBackdrop');
    const btn = document.getElementById('sidebarToggleBtn');
    const icon = document.getElementById('sidebarToggleIcon');
    if (!sidebar) return;
    sidebar.classList.remove('open');
    if (backdrop) backdrop.classList.remove('show');
    if (btn) btn.setAttribute('aria-expanded', 'false');
    if (icon) icon.className = 'fa-solid fa-bars';
    document.body.style.overflow = '';
}

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeSidebar();
});

// A viewport resize past the mobile breakpoint (e.g. rotating to
// landscape, or a resizable desktop test window) should drop the
// drawer-open state - otherwise .sidebar's transform/open styling could
// carry over stale once the media query stops applying it.
window.addEventListener('resize', () => {
    if (window.innerWidth > 768) closeSidebar();
});

document.addEventListener('DOMContentLoaded', syncThemeToggleIcon);

// Pages that include a #logoutAnimationModal show it briefly before
// redirecting (its optional data-delay overrides the default); everywhere
// else signs out immediately.
function handleLogout() {
    if (!confirm("Are you sure you want to sign out of the system?")) return;
    const farewell = document.getElementById('logoutAnimationModal');
    if (!farewell) {
        window.location.href = '/logout';
        return;
    }
    farewell.style.display = 'flex';
    setTimeout(() => { window.location.href = '/logout'; }, Number(farewell.dataset.delay) || 2200);
}

function showToast(title, message, type = 'success') {
    const toast = document.getElementById('floating-toast');
    const toastTitle = document.getElementById('toast-title');
    const toastMsg = document.getElementById('toast-message');
    const toastIcon = document.getElementById('toast-icon');
    if (!toast || !toastTitle || !toastMsg || !toastIcon) return;

    toast.className = '';
    toast.classList.add(type);
    toastTitle.innerText = title;
    toastMsg.innerText = message;
    toastIcon.innerHTML = type === 'success'
        ? '<i class="fa-solid fa-circle-check" style="color: var(--accent-green);"></i>'
        : '<i class="fa-solid fa-circle-exclamation" style="color: var(--accent-red);"></i>';
    toast.classList.add('show');

    setTimeout(() => { toast.classList.remove('show'); }, 3500);
}

// Escapes a value for safe insertion as HTML text/attribute content.
function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// Escapes a value for safe insertion as a single-quoted JS string literal
// argument inside an onclick="...('...')" attribute. HTML-escaping alone
// is NOT enough here: the browser HTML-decodes the attribute value back
// to raw text before handing it to the JS engine, so an HTML-escaped
// quote (&#39;) still decodes back into a literal ' and breaks out of
// the string. This escapes backslashes/quotes for the JS parser AND
// escapes double quotes for the surrounding HTML attribute boundary.
function escapeJsString(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'")
        .replace(/"/g, '&quot;')
        .replace(/\n/g, '\\n')
        .replace(/\r/g, '');
}

// No more dropdown-closing logic here - the "Menu" dropdown it used to
// close is gone entirely, replaced by the always-visible sidebar.
window.onclick = function(event) {
    if (event.target.classList.contains('modal-overlay')) {
        event.target.classList.remove('open');
    }
};

function openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) modal.classList.add('open');
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) modal.classList.remove('open');
}

// --- IN-APP NOTIFICATION CENTER (bell icon in base.html's header) ---
// Independent of email/Gmail preferences by design (see the backend's
// utils.create_notification). New notifications arrive as live updates
// (see startLiveUpdates below); the slow poll only covers a dropped
// stream. Only runs at all on pages where the bell exists (base.html only
// renders it for a logged-in session), so anonymous pages like /login do
// nothing here.
let notifPollTimer = null;

function initNotificationCenter() {
    const bell = document.getElementById('notifBellBtn');
    if (!bell) return; // not logged in - nothing to poll
    refreshNotifications();
    if (notifPollTimer) clearInterval(notifPollTimer);
    notifPollTimer = setInterval(refreshNotifications, 2 * LIVE_FALLBACK_POLL_MS);

    document.addEventListener('click', (e) => {
        const wrap = document.querySelector('.notif-bell-wrap');
        const panel = document.getElementById('notifPanel');
        if (!wrap || !panel || !panel.classList.contains('show')) return;
        if (!wrap.contains(e.target)) closeNotifPanel();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeNotifPanel();
    });
}

function toggleNotifPanel() {
    const panel = document.getElementById('notifPanel');
    if (!panel) return;
    if (panel.classList.contains('show')) {
        closeNotifPanel();
    } else {
        panel.classList.add('show');
        document.getElementById('notifBellBtn')?.setAttribute('aria-expanded', 'true');
        refreshNotifications();
    }
}

function closeNotifPanel() {
    const panel = document.getElementById('notifPanel');
    if (!panel) return;
    panel.classList.remove('show');
    document.getElementById('notifBellBtn')?.setAttribute('aria-expanded', 'false');
}

function refreshNotifications() {
    fetch('/api/notifications?limit=30')
        .then(res => res.ok ? res.json() : null)
        .then(data => {
            if (!data || data.status !== 'success') return;
            renderNotifBadge(data.unread_count || 0);
            renderNotifList(data.data || []);
        })
        .catch(() => {});
}

function renderNotifBadge(unreadCount) {
    const badge = document.getElementById('notifBadge');
    if (!badge) return;
    if (unreadCount > 0) {
        badge.hidden = false;
        badge.innerText = unreadCount > 99 ? '99+' : String(unreadCount);
    } else {
        badge.hidden = true;
    }
}

function renderNotifList(items) {
    const list = document.getElementById('notifPanelList');
    if (!list) return;
    if (!items.length) {
        list.innerHTML = '<div class="notif-empty"><i class="fa-regular fa-bell-slash"></i> No notifications yet.</div>';
        return;
    }
    list.innerHTML = items.map(n => `
        <div class="notif-item ${n.is_read ? '' : 'unread'}" onclick="handleNotifClick(${n.notification_id}, this)">
            <div class="notif-item-title">${escapeHtml(n.title)}</div>
            <div class="notif-item-message">${escapeHtml(n.message)}</div>
            <div class="notif-item-time">${escapeHtml(formatRelativeTime(n.created_at))}</div>
        </div>
    `).join('');
}

function handleNotifClick(notificationId, el) {
    if (el && el.classList.contains('unread')) {
        el.classList.remove('unread');
        fetch('/api/notifications/mark-read', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ notification_id: notificationId })
        })
        .then(() => refreshNotifications())
        .catch(() => {});
    }
}

function markAllNotificationsRead() {
    fetch('/api/notifications/mark-all-read', { method: 'POST', headers: { 'Content-Type': 'application/json' } })
        .then(() => refreshNotifications())
        .catch(() => {});
}

// Coarse relative-time label ("Just now" / "5m ago" / "3h ago" / "2d ago")
// for a 'YYYY-MM-DD HH:MM:SS' timestamp - notifications don't need
// second-level precision, just a quick sense of how recent they are.
function formatRelativeTime(sqlTimestamp) {
    if (!sqlTimestamp) return '';
    const then = parseServerDateTime(sqlTimestamp);
    if (!then) return sqlTimestamp;
    const diffMs = Date.now() - then;
    const mins = Math.floor(diffMs / 60000);
    if (mins < 1) return 'Just now';
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
}

function parseServerDateTime(sqlTimestamp) {
    // 'YYYY-MM-DD HH:MM:SS' -> epoch ms, treated as local time (matches how
    // this app writes every timestamp - see database.py's created_at
    // comments), or null if missing/malformed. A plain
    // `new Date('YYYY-MM-DD HH:MM:SS')` is not reliably parsed across
    // browsers, so this is built explicitly.
    const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/.exec(sqlTimestamp || '');
    if (!m) return null;
    const [, y, mo, d, h, mi, s] = m.map(Number);
    return new Date(y, mo - 1, d, h, mi, s).getTime();
}

document.addEventListener('DOMContentLoaded', initNotificationCenter);

// --- KPI STRIP (admin-dashboard.html and homepage.html) ---
// Fills `${prefix}-queue-count`, `-completed-today`, `-avg-wait-today` and
// `-active-staff` from /api/admin/metrics. Resolves true once rendered,
// false if the server answered with an error; rejects on a network error.
function loadKpiStrip(prefix) {
    return fetch('/api/admin/metrics')
        .then(res => res.json())
        .then(data => {
            if (data.status !== 'success') return false;
            document.getElementById(`${prefix}-queue-count`).innerText = data.queue_count;
            document.getElementById(`${prefix}-completed-today`).innerText = data.completed_today;
            document.getElementById(`${prefix}-avg-wait-today`).innerHTML = (data.avg_wait_today_minutes !== null && data.avg_wait_today_minutes !== undefined)
                ? `${data.avg_wait_today_minutes} <small>min</small>`
                : '<small>No completions yet today</small>';
            document.getElementById(`${prefix}-active-staff`).innerText = data.active_staff;
            return true;
        });
}

// --- BOOKING TIME SLOTS (booking.html's picker, homepage.html's calendar) ---
// MD57 is open 8:00 AM - 9:00 PM (utils.py's is_within_business_hours
// enforces the same window server-side).
const BOOKING_TIME_SLOTS = [
    '08:00 AM', '09:00 AM', '10:00 AM', '11:00 AM',
    '12:00 PM', '01:00 PM', '02:00 PM', '03:00 PM',
    '04:00 PM', '05:00 PM', '06:00 PM', '07:00 PM',
    '08:00 PM', '09:00 PM'
];

// '02:00 PM' -> 840 (minutes since midnight).
function slotTimeToMinutes(timeStr) {
    let [timePart, modifier] = timeStr.split(' ');
    let [hours, minutes] = timePart.split(':').map(Number);
    if (modifier === 'PM' && hours < 12) hours += 12;
    if (modifier === 'AM' && hours === 12) hours = 0;
    return hours * 60 + minutes;
}

// --- SERVICE / PRICING CATALOG ---
// /api/services is the one real source of truth for what services exist,
// what each costs per vehicle size, and which bay it routes to - shared by
// the Pricing modal below, the booking form (booking.html), and the
// walk-in modal (queue_dashboard.html), so all three can never drift out
// of sync with each other or with the shop's real published price list the
// way the old hardcoded "3 services"/"4 vehicle types" copies eventually did.
function formatPeso(amount) {
    if (amount === null || amount === undefined || amount === '') return 'N/A';
    return '₱' + Number(amount).toLocaleString('en-PH', { maximumFractionDigits: 0 });
}

async function fetchServiceCatalog() {
    const res = await fetch('/api/services');
    const data = await res.json();
    if (data.status !== 'success') throw new Error(data.message || 'Failed to load services.');
    return data;
}

function populateVehicleTypeSelect(selectId, catalog) {
    const select = document.getElementById(selectId);
    if (!select) return;
    select.innerHTML = catalog.vehicle_types.map(vt =>
        `<option value="${escapeHtml(vt.type_name)}">${escapeHtml(vt.type_name)}</option>`
    ).join('');
}

// Only ever lists services that actually have a price for the given
// vehicle_type - a motorcycle-only service never appears once a car size
// is selected, and vice versa, so the dropdown can't offer a combination
// /api/submit-booking would reject.
function populateServiceSelectForVehicleType(selectId, catalog, vehicleType) {
    const select = document.getElementById(selectId);
    if (!select || !catalog || !vehicleType) return;
    let html = '';
    Object.values(catalog.categories).forEach(cat => {
        const available = cat.services.filter(svc => vehicleType in svc.prices);
        if (available.length === 0) return;
        const options = available.map(svc =>
            `<option value="${svc.service_id}">${escapeHtml(svc.name)} (${escapeHtml(svc.default_bay)})</option>`
        ).join('');
        html += `<optgroup label="${escapeHtml(cat.label)}">${options}</optgroup>`;
    });
    select.innerHTML = html || '<option value="">No services available for this vehicle size</option>';
}

function findServiceInCatalog(catalog, serviceId) {
    if (!catalog || !serviceId) return null;
    for (const cat of Object.values(catalog.categories)) {
        const found = cat.services.find(s => String(s.service_id) === String(serviceId));
        if (found) return found;
    }
    return null;
}

// --- PRICING MODAL (base.html - available from every page's sidebar) ---
// Rendered dynamically from the same /api/services catalog above (fetched
// once, the first time the modal is opened) instead of hardcoded prices in
// the template - the whole point of storing pricing in the database is
// that an admin can update it from the pricing management page without
// touching any template.
let _pricingModalCatalog = null;

// Deliberately does not call the generic openModal()/closeModal() helpers -
// some pages (homepage.html, login.html, register.html) redefine those
// two names locally for their own
// unrelated modals (a plain style.display toggle, not the .open class this
// modal's CSS actually keys off), which silently shadows the shared
// versions for the whole page and left this modal invisible (opacity still
// 0) despite "opening" on those pages. Manipulating the class directly
// here means it behaves identically on every page regardless of what that
// page does with the generic names.
async function openPricingModal() {
    const modal = document.getElementById('pricingModal');
    if (modal) modal.classList.add('open');
    if (_pricingModalCatalog) return; // already loaded this session
    const body = document.getElementById('pricingModalBody');
    if (!body) return;
    try {
        _pricingModalCatalog = await fetchServiceCatalog();
        renderPricingModalCategory('general');
    } catch (err) {
        body.innerHTML = '<p style="text-align:center; color: var(--text-muted); padding: 20px 0;">Could not load pricing right now. Please try again later.</p>';
    }
}

function renderPricingModalCategory(categoryKey) {
    const body = document.getElementById('pricingModalBody');
    if (!body || !_pricingModalCatalog) return;
    const cat = _pricingModalCatalog.categories[categoryKey];
    if (!cat) return;

    const sizes = _pricingModalCatalog.vehicle_types.map(vt => vt.type_name)
        .filter(size => cat.services.some(svc => size in svc.prices));

    const tieredServices = cat.services.filter(svc => !svc.fixed_price);
    const fixedServices = cat.services.filter(svc => svc.fixed_price);

    let html = '';
    if (tieredServices.length) {
        html += '<div class="pricing-table-wrap"><table class="pricing-table"><thead><tr><th>Service</th>' +
            sizes.map(s => `<th>${escapeHtml(s)}</th>`).join('') + '</tr></thead><tbody>' +
            tieredServices.map(svc => `<tr><td>${escapeHtml(svc.name)}</td>` +
                sizes.map(s => `<td>${s in svc.prices ? formatPeso(svc.prices[s]) : '&mdash;'}</td>`).join('') +
                '</tr>').join('') +
            '</tbody></table></div>';
    }
    if (fixedServices.length) {
        html += `<h4 class="pricing-fixed-title">Fixed Price &mdash; All Vehicle Types</h4><ul class="pricing-fixed-list">` +
            fixedServices.map(svc => `<li><span>${escapeHtml(svc.name)}</span><strong>${formatPeso(Object.values(svc.prices)[0])}</strong></li>`).join('') +
            `</ul>`;
    }
    if (!tieredServices.length && !fixedServices.length) {
        html = '<p style="text-align:center; color: var(--text-muted); padding: 20px 0;">No services in this category yet.</p>';
    }

    document.getElementById('pricingModalBody').innerHTML = html;
}

function switchPricingTab(tabKey, btnEl) {
    document.querySelectorAll('.pricing-tab-btn').forEach(b => b.classList.toggle('active', b === btnEl));
    renderPricingModalCategory(tabKey);
}

// See openPricingModal's comment on why this manipulates the class
// directly instead of calling the generic closeModal() - that name is
// shadowed with a different (style.display-only) implementation on
// several pages, which would leave this modal not actually hidden again.
function closePricingModal() {
    const modal = document.getElementById('pricingModal');
    if (modal) modal.classList.remove('open');
}

// Maps a queue/history/ready item to which of the 3 physical bays it
// belongs to: its real assigned_bay, or - only for a row with no
// recognizable bay - a guess from the service name.
function getNormalizedBayKey(item) {
    const bayStr = (item.assigned_bay || "").toLowerCase();
    for (const bay of ['Bay #1', 'Bay #2', 'Bay #3']) {
        if (bayStr.includes(bay.toLowerCase())) return bay;
    }
    const serviceStr = (item.service_type || "").toLowerCase();
    if (serviceStr.includes('detailing') || serviceStr.includes('coating') || serviceStr.includes('full body')) return "Bay #3";
    if (serviceStr.includes('express') || serviceStr.includes('foam')) return "Bay #2";
    return "Bay #1";
}

// Local YYYY-MM-DD, matching booking_date/date_str's stored format - not
// toISOString(), which is UTC and lands on the WRONG calendar day for
// roughly a third of every day in this app's own timezone (PH, UTC+8):
// any local time between midnight and 8:00 AM still reads as "yesterday"
// in UTC. Was previously defined correctly only inside
// queue_dashboard.html (as a local, page-only function) while five other
// call sites elsewhere (queue_dashboard.html itself twice more, plus
// staff.html three times and history.html once) kept using the buggy
// toISOString() version - including staff.html's `currentDateStr`, the
// single value that decides which day's attendance the whole Staff page
// reads and renders "on-duty" from. That's what caused the Active Staff
// KPI (server-computed, correctly local) to disagree with the Staff
// page's own on-duty list (browser-computed, previously UTC) whenever
// viewed before 8 AM local time. Now the one shared definition every page
// uses.
function getTodayDateStr() {
    return getDateStrOffset(0);
}

// Same local-date logic, offset by `days` (e.g. getDateStrOffset(1) for
// "tomorrow") - the toISOString()-based `new Date(Date.now() + 86400000)`
// pattern this replaces had the identical UTC bug as getTodayDateStr did.
function getDateStrOffset(days) {
    let d = new Date();
    d.setDate(d.getDate() + days);
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}

// --- LIVE UPDATES (Server-Sent Events - see the backend's live_updates.py) ---
// The server pushes 'data' whenever something pages display changes (on
// any device, not just this browser) and 'notifications' when this
// account gets one. Pages register what to re-fetch with onLiveUpdate();
// its slow fallback poll covers any gap while the stream reconnects.
const LIVE_FALLBACK_POLL_MS = 60000;

function onLiveUpdate(refresh) {
    document.addEventListener('md57-data-changed', refresh);
    setInterval(refresh, LIVE_FALLBACK_POLL_MS);
}

function startLiveUpdates() {
    if (!document.getElementById('notifBellBtn') || !window.EventSource) return; // not logged in
    if (document.body.dataset.liveUpdates === 'off') return;
    const source = new EventSource('/api/live');
    let refreshTimer = null;
    source.addEventListener('data', () => {
        // One action often announces several changes - refresh once.
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(() => document.dispatchEvent(new CustomEvent('md57-data-changed')), 250);
    });
    source.addEventListener('notifications', refreshNotifications);
}

document.addEventListener('DOMContentLoaded', startLiveUpdates);

// Shared by every CSV export button on this site - the column choice
// stays page-specific; quoting and the download itself happen here.
function downloadCSV(filename, header, rows) {
    const cell = value => {
        const text = value === null || value === undefined ? '' : String(value);
        return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    };
    const csv = [header, ...rows].map(row => row.map(cell).join(',')).join('\r\n');
    const url = URL.createObjectURL(new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
}

// Generic multi-step form navigator - powers the stepper markup (see
// style.css's .stepper/.stepper-step/.form-step rules) on both
// booking.html and create_user.html. register.html has its own inline
// copy of this same logic since it's a standalone page that doesn't load
// common.js (same reason it duplicates toggleTheme/etc).
//
// Expects, inside `formEl`: one `.form-step[data-step="N"]` per step (step
// 1 visible, the rest starting `style="display:none"` in the markup), a
// matching `.stepper-step[data-step="N"]` per step in the indicator, and
// `.stepper-back-btn` / `.stepper-next-btn` / `.stepper-submit-btn`
// buttons. `validateStepFn(stepNumber)` is caller-supplied (each form's
// required fields differ) - return false and show your own toast to
// block advancing past an incomplete step; omit it to never block.
// `onStepShown(stepNumber)` fires after every render (e.g. to refresh a
// review-step summary right before it's shown).
function initStepper(formEl, totalSteps, validateStepFn, onStepShown) {
    let current = 1;

    function render() {
        formEl.querySelectorAll('.form-step').forEach(el => {
            el.style.display = (parseInt(el.dataset.step, 10) === current) ? '' : 'none';
        });
        formEl.querySelectorAll('.stepper-step').forEach(el => {
            const n = parseInt(el.dataset.step, 10);
            el.classList.toggle('active', n === current);
            el.classList.toggle('completed', n < current);
        });
        const backBtn = formEl.querySelector('.stepper-back-btn');
        const nextBtn = formEl.querySelector('.stepper-next-btn');
        const submitBtn = formEl.querySelector('.stepper-submit-btn');
        if (backBtn) backBtn.style.display = current === 1 ? 'none' : '';
        if (nextBtn) nextBtn.style.display = current === totalSteps ? 'none' : '';
        if (submitBtn) submitBtn.style.display = current === totalSteps ? '' : 'none';
        if (typeof onStepShown === 'function') onStepShown(current);
        formEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function next() {
        if (typeof validateStepFn === 'function' && !validateStepFn(current)) return;
        if (current < totalSteps) { current++; render(); }
    }
    function back() {
        if (current > 1) { current--; render(); }
    }

    const backBtn = formEl.querySelector('.stepper-back-btn');
    const nextBtn = formEl.querySelector('.stepper-next-btn');
    if (backBtn) backBtn.addEventListener('click', back);
    if (nextBtn) nextBtn.addEventListener('click', next);

    render();
    return { next, back, current: () => current };
}
