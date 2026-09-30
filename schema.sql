-- Schema for the course-enrollment app in student_course_mgmt.py.
--
-- The ``students`` table is owned by student_mgmt.py and is not defined here;
-- these tables reference it. Run this file once against the database named by
-- DATABASE_URL in .env:
--
--     psql "$DATABASE_URL" -f schema.sql
--
-- Every statement is idempotent, so re-running it is safe. The app itself
-- issues no DDL at import or startup.

-- A catalogue of courses. ``name`` is unique so a course cannot be defined
-- twice under slightly different spellings, and the enrollment routes report
-- that clash as 409.
CREATE TABLE IF NOT EXISTS courses (
    id          serial       PRIMARY KEY,
    name        varchar(100) NOT NULL UNIQUE,
    description varchar(500)
);

-- One row per student/course pairing.
--
-- The UNIQUE constraint is the real guarantee that a student cannot enroll in
-- the same course twice: it holds even when two concurrent requests both pass
-- the application's pre-insert existence check. The application checks first so
-- it can return a friendly 409, and the constraint backstops it.
--
-- Both foreign keys cascade, so deleting a student or a course clears the
-- enrollments that referenced it instead of leaving them dangling.
CREATE TABLE IF NOT EXISTS enrollments (
    id          serial      PRIMARY KEY,
    student_id  integer     NOT NULL REFERENCES students (id) ON DELETE CASCADE,
    course_id   integer     NOT NULL REFERENCES courses  (id) ON DELETE CASCADE,
    enrolled_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_enrollments_student_course UNIQUE (student_id, course_id)
);

-- Supports listing a course's roster; the UNIQUE constraint already indexes
-- (student_id, course_id) for the per-student lookup used by the list endpoint.
CREATE INDEX IF NOT EXISTS ix_enrollments_course_id ON enrollments (course_id);