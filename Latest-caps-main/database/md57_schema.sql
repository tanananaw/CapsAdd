-- MD57 Car Wash & Auto Detailing - Database Schema
-- Dialect: SQLite 3

PRAGMA foreign_keys = ON;

-- 1. Users & Roles (RBAC)
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'staff', 'customer')),
    fullname TEXT,
    auth_provider TEXT, -- 'google' for Sign-In-with-Google accounts, NULL otherwise; password login is refused when set
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Login brute-force lockout tracking (login_security.py): one row per
-- attempt, success or failure, checked independently by username and by
-- IP within a sliding window. Was missing from this reference file even
-- though it's been live in database.py since the login-lockout feature.
CREATE TABLE IF NOT EXISTS login_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    ip_address TEXT,
    success INTEGER NOT NULL DEFAULT 0,
    attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_username ON login_attempts (username, attempted_at);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts (ip_address, attempted_at);

-- 2. Services
CREATE TABLE IF NOT EXISTS services (
    service_id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_name TEXT NOT NULL,
    service_type TEXT NOT NULL,
    base_duration_minutes INTEGER NOT NULL,
    price REAL NOT NULL
);

-- 3. Vehicle Types & Wait Multipliers
CREATE TABLE IF NOT EXISTS vehicle_types (
    type_id INTEGER PRIMARY KEY AUTOINCREMENT,
    type_name TEXT NOT NULL UNIQUE,
    multiplier_minutes INTEGER NOT NULL DEFAULT 0
);

-- 4. Reservations & Queue Management
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    customer_name TEXT,
    vehicle_plate_number TEXT NOT NULL,
    vehicle_type TEXT NOT NULL,
    service_id INTEGER,
    customer_email TEXT,
    queue_number INTEGER,
    queue_status TEXT DEFAULT 'waiting', -- 'waiting', 'in_progress', 'ready', 'completed', 'cancelled'
    booking_source TEXT DEFAULT 'Walk-in Customer', -- 'Online Booking', 'Walk-in Customer'
    assigned_bay TEXT DEFAULT 'Bay #1',
    booking_date TEXT,
    booking_time TEXT,
    cancelled_at TEXT,
    completed_at TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    queue_length_snapshot INTEGER, -- same-service active queue depth at booking time (AI retraining)
    active_staff_snapshot INTEGER, -- staff clocked in at booking time (AI retraining)
    assigned_staff_id INTEGER, -- staff who started this job (real commission attribution)
    predicted_wait_minutes REAL, -- model's own prediction at booking time (real MAE/RMSE tracking)
    started_at TIMESTAMP, -- when a staff member actually started the job (real Speed metric)
    customer_phone TEXT, -- for SMS notifications, captured per-booking like customer_email
    notify_via TEXT, -- 'email' (default/NULL), 'sms', or 'both' - the customer's chosen channel(s)
    FOREIGN KEY (service_id) REFERENCES services(service_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (assigned_staff_id) REFERENCES users(user_id)
);

-- 5. Staff Attendance
CREATE TABLE IF NOT EXISTS staff_attendance (
    attendance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_name TEXT NOT NULL,
    date_str TEXT NOT NULL,
    status TEXT DEFAULT 'Absent',
    time_in TEXT DEFAULT '--:--',
    time_out TEXT DEFAULT '--:--',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 6. Staff Bonuses
CREATE TABLE IF NOT EXISTS staff_bonuses (
    bonus_id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_name TEXT NOT NULL,
    amount REAL NOT NULL,
    reason TEXT,
    timestamp TEXT NOT NULL
);

-- 6b. Staff Shift Roster (forward-looking schedule - separate from
-- staff_attendance, which only records what already happened)
CREATE TABLE IF NOT EXISTS staff_shifts (
    shift_id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_name TEXT NOT NULL,
    shift_date TEXT NOT NULL,
    shift_note TEXT,
    created_by TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_staff_shifts_unique ON staff_shifts(staff_name, shift_date);
CREATE INDEX IF NOT EXISTS idx_staff_shifts_date ON staff_shifts(shift_date);

-- 7. Customer Feedback
CREATE TABLE IF NOT EXISTS feedbacks (
    feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER,
    user_id INTEGER,
    customer_name TEXT NOT NULL,
    plate_number TEXT NOT NULL,
    service_type TEXT NOT NULL,
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    comment TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (reservation_id) REFERENCES reservations(reservation_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- One review per reservation (NULL reservation_id exempt - legacy/seed rows)
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_reservation_unique
    ON feedbacks (reservation_id) WHERE reservation_id IS NOT NULL;

-- 8. Password Resets (self-service "Forgot password" flow)
CREATE TABLE IF NOT EXISTS password_resets (
    reset_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets(token);
CREATE INDEX IF NOT EXISTS idx_password_resets_user ON password_resets(user_id, used, created_at);

-- Prevents double-booking the same bay/date/time slot at the database
-- level (the real guarantee behind the booking race-condition fix) - walk-
-- ins (NULL booking_date/time) are exempt since SQL never treats NULLs as
-- equal to each other. Was missing from this reference file entirely.
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_slot_unique
    ON reservations (assigned_bay, booking_date, booking_time)
    WHERE queue_status IN ('waiting', 'in_progress');

-- Indexes for performance (now real - also created by database.py's
-- init_db(), which is what actually runs; this file is documentation only)
CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservations(queue_status);
CREATE INDEX IF NOT EXISTS idx_reservations_plate ON reservations(vehicle_plate_number);
CREATE INDEX IF NOT EXISTS idx_reservations_date ON reservations(booking_date);
-- Not created explicitly by database.py: users.username is UNIQUE above,
-- and SQLite already builds an implicit index for that on its own.
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);