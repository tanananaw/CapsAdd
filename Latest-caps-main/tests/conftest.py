import atexit
import contextlib
import io
import os
import shutil
import sys
import tempfile

# pytest imports this before any test module, so every `from app import ...`
# below it sees these paths - the suite gets a fresh, seeded throwaway
# database and audit log instead of writing into the real md57_carwash.db
# and system_activity.log (see config.py's DB_FILE/ACTIVITY_LOG_FILE).
_TEST_DATA_DIR = tempfile.mkdtemp(prefix='md57_tests_')
os.environ['MD57_DB_FILE'] = os.path.join(_TEST_DATA_DIR, 'md57_test.db')
os.environ['MD57_ACTIVITY_LOG'] = os.path.join(_TEST_DATA_DIR, 'system_activity.log')

atexit.register(shutil.rmtree, _TEST_DATA_DIR, ignore_errors=True)

# Create and seed the schema up front - test files that only import
# database/utils (not app, which would do it) still get real tables.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '_Backend'))
from database import init_db  # noqa: E402

with contextlib.redirect_stdout(io.StringIO()):
    init_db()
