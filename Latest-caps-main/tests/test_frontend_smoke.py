import unittest
import os
import sys
import threading
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# A dedicated port, not the one a developer runs the app on manually while
# working (5000) - these tests boot their own throwaway server instance so
# they never fight over the port with someone actively using the app.
TEST_PORT = 5099
BASE_URL = f'http://127.0.0.1:{TEST_PORT}'

ADMIN_USERNAME = 'manager'
ADMIN_PASSWORD = os.environ.get('DEFAULT_ADMIN_PASSWORD', 'md57admin2026')


def _run_server():
    app.run(host='127.0.0.1', port=TEST_PORT, debug=False, use_reloader=False)


@unittest.skipUnless(PLAYWRIGHT_AVAILABLE, "playwright not installed - run 'python -m pip install playwright && python -m playwright install chromium'")
class FrontendSmokeTestCase(unittest.TestCase):
    """Browser-driven smoke tests using a real headless Chromium (via
    Playwright), not the Flask test client the rest of this suite uses.

    Why this file exists: every other test in tests/ calls Flask routes
    directly and asserts on the returned HTML/JSON - it never actually
    parses or executes the page's CSS/JS the way a browser does. That gap
    is exactly how the dark/light theme system was silently dropped
    entirely during a UI rewrite (see ROADMAP.md item 36) with all 179
    backend tests still green the whole time - nothing was checking that
    a real browser could still toggle the theme and see the page change.
    This file is not meant to be exhaustive UI coverage; it's a small set
    of "if this breaks, something is seriously wrong" checks for the
    exact failure modes this project has already hit for real."""

    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        # Pages otherwise hold a live-updates stream open, which Playwright's
        # 'networkidle' would wait on forever (live updates have their own
        # tests in test_live_updates.py).
        app.config['LIVE_UPDATES'] = False

        cls.server_thread = threading.Thread(target=_run_server, daemon=True)
        cls.server_thread.start()

        for _ in range(50):
            try:
                urllib.request.urlopen(BASE_URL + '/login', timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError(f'Test server did not start on {BASE_URL} in time')

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.console_errors = []
        self.page.on('console', lambda msg: self.console_errors.append(msg.text) if msg.type == 'error' else None)
        self.page.on('pageerror', lambda exc: self.console_errors.append(str(exc)))

    def tearDown(self):
        self.page.close()

    def _login_as_admin(self):
        self.page.goto(BASE_URL + '/login')
        self.page.fill('#login_user', ADMIN_USERNAME)
        self.page.fill('#login_pass', ADMIN_PASSWORD)
        self.page.click('button[type="submit"]')
        self.page.wait_for_load_state('networkidle')

    # --- Page load / JS syntax sanity ---

    def test_login_page_loads_without_console_errors(self):
        self.page.goto(BASE_URL + '/login')
        self.assertIn('MD57', self.page.title())
        self.assertEqual(self.console_errors, [])

    def test_login_succeeds_and_reaches_an_authenticated_page(self):
        self._login_as_admin()
        self.assertNotIn('/login', self.page.url)
        self.assertEqual(self.console_errors, [])

    def test_key_authenticated_pages_load_without_console_errors(self):
        self._login_as_admin()
        pages = ['/', '/queue', '/admin', '/staff', '/history', '/cancelled', '/ready', '/analytics', '/feedback', '/book']
        for path in pages:
            with self.subTest(path=path):
                self.console_errors.clear()
                self.page.goto(BASE_URL + path)
                self.page.wait_for_load_state('networkidle')
                self.assertEqual(self.console_errors, [], f'{path} produced console errors: {self.console_errors}')

    # --- Dark mode: the exact regression class this file was built to catch ---

    def test_dark_mode_toggle_actually_changes_the_rendered_page(self):
        self._login_as_admin()
        self.page.goto(BASE_URL + '/')
        self.page.wait_for_selector('.theme-toggle-btn')

        # backgroundImage, not backgroundColor: body's background is a
        # watermark-logo gradient overlay in BOTH themes now (see
        # style.css's html,body rule), with only the gradient's tint
        # differing per theme via --bg-overlay - backgroundColor itself is
        # transparent in both and would no longer catch a regression here.
        light_bg = self.page.evaluate("getComputedStyle(document.body).backgroundImage")
        theme_attr_before = self.page.evaluate("document.documentElement.getAttribute('data-theme')")
        self.assertIsNone(theme_attr_before, "page should start in light mode with no data-theme set")

        self.page.click('.theme-toggle-btn')
        self.page.wait_for_timeout(150)  # CSS transition

        theme_attr_after = self.page.evaluate("document.documentElement.getAttribute('data-theme')")
        dark_bg = self.page.evaluate("getComputedStyle(document.body).backgroundImage")

        self.assertEqual(theme_attr_after, 'dark')
        self.assertNotEqual(light_bg, dark_bg, "toggling dark mode must actually change the rendered background, not just an attribute nobody reads")

    def test_dark_mode_preference_persists_across_page_loads(self):
        self._login_as_admin()
        self.page.goto(BASE_URL + '/')
        self.page.click('.theme-toggle-btn')
        self.page.wait_for_timeout(150)

        self.page.goto(BASE_URL + '/queue')
        theme_attr = self.page.evaluate("document.documentElement.getAttribute('data-theme')")
        self.assertEqual(theme_attr, 'dark', "the FOUC-prevention script should re-apply the saved theme on every new page load")

    # --- Pagination: the other repeat-regression class in this project ---

    def test_cancelled_page_pagination_controls_render(self):
        self._login_as_admin()
        self.page.goto(BASE_URL + '/cancelled')
        self.page.wait_for_selector('#page-info-bay-1')
        page_info_text = self.page.inner_text('#page-info-bay-1')
        self.assertRegex(page_info_text.strip(), r'^\d+\s*/\s*\d+$', "pagination should render as 'N / M', not be blank or broken")


if __name__ == '__main__':
    unittest.main()
