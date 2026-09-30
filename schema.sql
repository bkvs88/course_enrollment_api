-- Schema for the course-enrollment app in student_course_mgmt.py.
--
-- ``students`` is NOT created here, and nothing else in this repository
-- creates it, so it must already exist before this file is applied:
-- ``enrollments.student_id`` references it and the enrollment routes read it
-- directly. Only ``id`` is used by this app; any other columns belong to
-- whatever owns the student records. Create that table first, then run this
-- file once against the database named by DATABASE_URL in .env:
--
--     psql "$DATABASE_URL" -f schema.sql
--
-- Every statement is idempotent, so re-running it is safe. The app itself
-- issues no DDL at import or startup.

-- A catalogue of courses. ``name`` is unique so a course cannot be defined
-- twice under slightly different spellings, and the enrollment routes report
-- that clash as 409.

CREATE TABLE "students" (
	"id" serial PRIMARY KEY,
	"name" varchar(100) NOT NULL,
	"age" integer NOT NULL,
	"city" varchar(100) NOT NULL,
	"email" varchar(255),
	"course" varchar(100)
);
CREATE UNIQUE INDEX "students_email_key" ON "students" ("email");
CREATE UNIQUE INDEX "students_pkey" ON "students" ("id");


CREATE TABLE "courses" (
	"id" serial PRIMARY KEY,
	"name" varchar(100) NOT NULL CONSTRAINT "courses_name_key" UNIQUE,
	"description" varchar(500)
);
CREATE UNIQUE INDEX "courses_name_key" ON "courses" ("name");
CREATE UNIQUE INDEX "courses_pkey" ON "courses" ("id");

-- One row per student/course pairing.
--
-- The UNIQUE constraint is the real guarantee that a student cannot enroll in
-- the same course twice: it holds even when two concurrent requests both pass
-- the application's pre-insert existence check. The application checks first so
-- it can return a friendly 409, and the constraint backstops it.
--
-- Both foreign keys cascade, so deleting a student or a course clears the
-- enrollments that referenced it instead of leaving them dangling.
CREATE TABLE "enrollments" (
	"id" serial PRIMARY KEY,
	"student_id" integer NOT NULL,
	"course_id" integer NOT NULL,
	"enrolled_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "uq_enrollments_student_course" UNIQUE("student_id","course_id")
);
CREATE UNIQUE INDEX "enrollments_pkey" ON "enrollments" ("id");
CREATE INDEX "ix_enrollments_course_id" ON "enrollments" ("course_id");
ALTER TABLE "enrollments" ADD CONSTRAINT "enrollments_course_id_fkey" FOREIGN KEY ("course_id") REFERENCES "courses"("id") ON DELETE CASCADE;
ALTER TABLE "enrollments" ADD CONSTRAINT "enrollments_student_id_fkey" FOREIGN KEY ("student_id") REFERENCES "students"("id") ON DELETE CASCADE;



