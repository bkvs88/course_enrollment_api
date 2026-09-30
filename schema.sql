-- Schema for the course-enrollment app in student_course_mgmt.py.
--
-- Three tables, in dependency order: ``students`` and ``courses`` are created
-- before ``enrollments`` because its foreign keys point at them. Those keys are
-- declared inline on the CREATE TABLE rather than added afterwards, so the
-- ordering above is the only thing that matters - there is no separate
-- ALTER TABLE step that could run against a not-yet-created parent.
--
-- Every statement is guarded with IF NOT EXISTS, so re-running the file is a
-- no-op rather than an error. That includes the indexes: the primary keys below
-- already build their own indexes, so there is deliberately no separate
-- CREATE INDEX for any ``*_pkey``; declaring one would collide with the
-- implicit one the same name.
--
-- The app itself issues no DDL at import or startup, so this file is the only
-- thing that creates the schema. Apply it once against the database named by
-- DATABASE_URL in .env:
--
--     psql "$DATABASE_URL" -f schema.sql

-- Students. Read-only as far as this app is concerned: the enrollment routes
-- look rows up to tell "no such student" (404) from "student with no
-- enrollments" (200), and never insert into it. The wider columns are carried
-- here so the table is usable on its own; this app reads only ``id``.
CREATE TABLE IF NOT EXISTS "students" (
	"id" serial PRIMARY KEY,
	"name" varchar(100) NOT NULL,
	"age" integer NOT NULL,
	"city" varchar(100) NOT NULL,
	"email" varchar(255) UNIQUE,
	"course" varchar(100)
);

-- A catalogue of courses. ``name`` is unique so a course cannot be defined
-- twice under slightly different spellings, and the enrollment routes report
-- that clash as 409.
--
-- The UNIQUE is declared inline, so its index comes with the table and needs no
-- separate CREATE INDEX here.
CREATE TABLE IF NOT EXISTS "courses" (
	"id" serial PRIMARY KEY,
	"name" varchar(100) NOT NULL CONSTRAINT "courses_name_key" UNIQUE,
	"description" varchar(500)
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
CREATE TABLE IF NOT EXISTS "enrollments" (
	"id" serial PRIMARY KEY,
	"student_id" integer NOT NULL REFERENCES "students" ("id") ON DELETE CASCADE,
	"course_id" integer NOT NULL REFERENCES "courses" ("id") ON DELETE CASCADE,
	"enrolled_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "uq_enrollments_student_course" UNIQUE ("student_id", "course_id")
);

-- Supports listing a course's roster. The UNIQUE constraint above already
-- indexes (student_id, course_id) for the per-student lookup used by the list
-- endpoint, so only the course_id direction needs an index of its own.
CREATE INDEX IF NOT EXISTS "ix_enrollments_course_id" ON "enrollments" ("course_id");
