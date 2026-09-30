"""Course enrollment API served by FastAPI and backed by PostgreSQL.

This module builds its own SQLAlchemy engine and session factory from the
``DATABASE_URL`` in the .env file, then registers the enrollment routes on a
FastAPI ``app``. It is a standalone application with no sibling module in this
repository.

Routes exposed by this module:

    POST   /courses                   - create a course.
    GET    /courses                   - list every course.
    POST   /enroll                    - enroll a student in a course.
    GET    /students/{student_id}/courses - list a student's courses.
    DELETE /enroll/{enrollment_id}    - cancel one enrollment.

Storage is two tables, both defined in ``schema.sql``:

    courses    (id, name, description)          name is unique.
    enrollments (id, student_id, course_id, enrolled_at)
                with UNIQUE (student_id, course_id).

``schema.sql`` creates ``students`` alongside these two, but the app itself
never writes to it: the enrollment routes read ``id`` only, and an id that is
not already present is reported as 404 rather than created implicitly.

Queries are written as raw parameterised SQL via ``text()``.

Double enrollment is prevented twice over. The handler first checks whether the
pairing exists and returns 409 with a readable message, and the
``uq_enrollments_student_course`` constraint is the authority behind that check:
two concurrent requests can both pass the check, but only one INSERT can commit.
The losing request's IntegrityError is caught and reported as the same 409.

**This module creates and migrates nothing.** It issues no ``CREATE TABLE`` and
calls no Alembic. ``schema.sql`` must already have been applied, or every route
fails with 500.

Run with: uvicorn student_course_mgmt:app --reload
"""

import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

# Reads DATABASE_URL (and any other keys) from the .env file next to this module.
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Returned verbatim for a unique-constraint violation. The driver message behind
# it names the index and the SQL, which is noise for a client, so the detail
# stays fixed rather than interpolating the original error.
DUPLICATE_COURSE_DETAIL = "Course name already exists"
DUPLICATE_ENROLLMENT_DETAIL = "Student is already enrolled in this course"

# The one message a client sees when a query fails for any reason we did not
# anticipate. Deliberately fixed and deliberately vague: the driver exception
# behind it spells out the failing statement, the table names and the constraint,
# which belongs in the server log rather than in an HTTP body. Every 500 handler
# below logs the real exception with ``logger.exception`` and returns this
# instead, so a database hiccup cannot leak schema details to a caller.
INTERNAL_ERROR_DETAIL = "Something went wrong while processing the request."


def check_database_connection():
    """Check that the configured database is reachable.

    Executes a trivial ``SELECT 1`` against the engine so connection problems
    surface at startup instead of on the first request.

    Returns:
        bool: True if the database responded, False if the attempt raised.
    """

    try:
        # Open a pooled connection just long enough to prove the credentials and
        # network path work; the connection is returned to the pool after.
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        print("Database connection successful")
    except Exception as e:
        print(f"Database connection failed: {e}")
        return False
    return True


check_database_connection()

app = FastAPI(
    title="Course Enrollment API",
    description="Course catalogue and student enrollment",
    version="1.0.0",
)


@app.get("/courses")
def get_courses():
    """Return every course in the catalogue.

    ``.mappings()`` turns each row into a plain dict keyed by column name, which
    FastAPI serialises directly to JSON. Row order is not guaranteed by the
    query, so callers must not depend on it.

    An empty catalogue answers with an explanatory object rather than a bare
    ``[]``, so a client reading the body can see why there is nothing in it
    instead of having to infer it. The status stays 200: the collection exists,
    it is simply unpopulated, so this is not a missing-resource error.

    Returns:
        list[dict] | dict: One dict per course when any exist, each holding id,
            name and description. When none exist, a dict holding a message
            pointing at POST /courses and a count of 0. Note the response is a
            JSON array in one case and a JSON object in the other.

    Raises:
        HTTPException: 500 if the query fails.
    """

    try:
        with SessionLocal() as session:
            result = session.execute(text("SELECT * FROM courses ORDER BY id"))
            courses = [dict(row) for row in result.mappings()]
    except Exception:
        # The real cause goes to the server log; the client gets the fixed
        # INTERNAL_ERROR_DETAIL and never the driver message.
        logger.exception("Error fetching courses")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)

    if not courses:
        return {
            "message": "No courses found. Add one with POST /courses.",
            "count": 0,
        }
    return courses


# Warm the query path at import time so a missing or broken table is noticed on
# startup. The result is discarded. The HTTPException is caught here on purpose:
# the route raises rather than returning None, and an uncaught one would
# propagate out of the import and leave the server unable to start at all. Since
# this module creates no schema, this is also the earliest point a missing
# ``courses`` table can be reported.
try:
    get_courses()
except HTTPException as e:
    print(f"Startup course query failed: {e.detail}")


# --- Request payloads ---------------------------------------------------------


class CourseCreate(BaseModel):
    """Payload for creating a course.

    Attributes:
        name: Course name, 2-100 characters, normalised by ``normalise_name``
            so that "  Data  Science " and "Data Science" are one course.
            Must be unique across the table.
        description: Optional free-text summary, at most 500 characters.
    """

    name: str = Field(min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def normalise_name(cls, v: str) -> str:
        """Collapse internal whitespace in a course name.

        So "  Data  Science " is stored as "Data Science", which keeps
        differently spaced spellings of the same course in one value. Case is
        left alone, so ``Python`` and ``python`` stay distinct courses.
        """

        return " ".join(v.split())


@app.post("/courses", status_code=201)
def create_course(course: CourseCreate):
    """Add a course to the catalogue.

    The ``id`` column is left to its sequence default, so rows are numbered by
    the database. The route is POST on /courses, so there is no id in the path.

    The created row is read back with ``RETURNING`` and returned in full. An
    enrollment needs the course id, and a client cannot learn it without a
    follow-up GET.

    Args:
        course: Validated name and optional description of the course to add.

    Returns:
        dict: The new row as a dict of column names to values - id, name and
            description - sent with status 201 Created as declared on the route
            decorator.

    Raises:
        HTTPException: 409 if the name is already taken, or 500 if the insert
            fails for any other reason.
    """

    try:
        with SessionLocal() as session:
            try:
                result = session.execute(
                    text(
                        "INSERT INTO courses (name, description) "
                        "VALUES (:name, :description) RETURNING *"
                    ),
                    {"name": course.name, "description": course.description},
                )
                created = dict(result.mappings().first())
                session.commit()
            except IntegrityError:
                # Raised by the unique index on courses.name. The pre-check is
                # unnecessary here: the database is the only arbiter of a name
                # clash, and letting it reject the row avoids a race that a
                # read-then-write would leave open.
                session.rollback()
                raise HTTPException(status_code=409, detail=DUPLICATE_COURSE_DETAIL)
    except HTTPException:
        raise
    except Exception:
        # The real cause goes to the server log; the client gets the fixed
        # INTERNAL_ERROR_DETAIL and never the driver message.
        logger.exception("Error creating course")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)

    return created


class EnrollCreate(BaseModel):
    """Payload for enrolling a student in a course.

        Attributes:
        student_id: Primary key of an existing row in the ``students`` table,
            at least 1. Read-only as far as this app is concerned.
        course_id: Primary key of an existing course, at least 1.
    """

    student_id: int = Field(ge=1)
    course_id: int = Field(ge=1)


@app.post("/enroll", status_code=201)
def enroll_student(enrollment: EnrollCreate):
    """Enroll a student in a course.

    A single lookup resolves all three questions at once - does the student
    exist, does the course exist, and is the pairing already enrolled - so the
    common rejections cost one round trip. The check is for a readable message
    only; the unique constraint on (student_id, course_id) is what actually
    prevents a double enrollment under concurrency.

    Args:
        enrollment: The student_id and course_id to pair.

    Returns:
        dict: The new enrollment as a dict of column names to values - id,
            student_id, course_id and enrolled_at - sent with status 201 Created
            as declared on the route decorator.

    Raises:
        HTTPException: 404 if the student or the course does not exist, 409 if
            the student is already enrolled in that course, or 500 if the
            insert fails for any other reason.
    """

    try:
        with SessionLocal() as session:
            try:
                # The subqueries return NULL rather than raising when a row is
                # missing, so one round trip covers all three checks. EXISTS
                # never returns NULL, which keeps already_enrolled strictly
                # boolean.
                state = session.execute(
                    text(
                        "SELECT "
                        "(SELECT id FROM students WHERE id = :student_id) AS student_id, "
                        "(SELECT id FROM courses WHERE id = :course_id) AS course_id, "
                        "EXISTS (SELECT 1 FROM enrollments "
                        "        WHERE student_id = :student_id AND course_id = :course_id) "
                        "AS already_enrolled"
                    ),
                    {
                        "student_id": enrollment.student_id,
                        "course_id": enrollment.course_id,
                    },
                ).mappings().first()

                if state["student_id"] is None:
                    raise HTTPException(
                        status_code=404, detail=f"Student {enrollment.student_id} not found"
                    )
                if state["course_id"] is None:
                    raise HTTPException(
                        status_code=404, detail=f"Course {enrollment.course_id} not found"
                    )
                if state["already_enrolled"]:
                    raise HTTPException(
                        status_code=409, detail=DUPLICATE_ENROLLMENT_DETAIL
                    )

                result = session.execute(
                    text(
                        "INSERT INTO enrollments (student_id, course_id) "
                        "VALUES (:student_id, :course_id) "
                        "RETURNING id, student_id, course_id, enrolled_at"
                    ),
                    {
                        "student_id": enrollment.student_id,
                        "course_id": enrollment.course_id,
                    },
                )
                created = dict(result.mappings().first())
                session.commit()
            except HTTPException:
                # Covers the 404s and 409 above as well as the 409 mapped from
                # IntegrityError below. Nothing was written, so there is nothing
                # to undo.
                session.rollback()
                raise
            except IntegrityError:
                # A concurrent request inserted the same pairing between the
                # check and the INSERT, so the unique constraint rejected this
                # row. Same 409 the check would have produced.
                session.rollback()
                raise HTTPException(status_code=409, detail=DUPLICATE_ENROLLMENT_DETAIL)
    except HTTPException:
        raise
    except Exception:
        # The real cause goes to the server log; the client gets the fixed
        # INTERNAL_ERROR_DETAIL and never the driver message.
        logger.exception("Error enrolling student")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)

    return created


@app.get("/students/{student_id}/courses")
def get_student_courses(student_id: int):
    """List the courses a student is enrolled in.

    The student is looked up separately from the enrollment list, because a
    student with no enrollments and a student who does not exist both produce an
    empty list, and only the second is a 404. A student who exists but holds no
    enrollments then gets the same explanatory object as an empty catalogue,
    naming the student so the caller can tell the two apart without re-reading
    the URL.

    Args:
        student_id: Primary key of the student to look up.

    Returns:
        list[dict] | dict: One dict per enrollment when any exist, each holding
            enrollment_id, course_id, course_name, course_description and
            enrolled_at. When the student has none, a dict holding a message,
            the student_id and a count of 0. Note the response is a JSON array
            in one case and a JSON object in the other.

    Raises:
        HTTPException: 404 if no student has that id, or 500 if the query fails.
    """

    try:
        with SessionLocal() as session:
            # Confirm the student exists before listing, so an unknown id is
            # distinguishable from a student who simply has no enrollments.
            exists = session.execute(
                text("SELECT 1 FROM students WHERE id = :id"), {"id": student_id}
            ).first()

            if not exists:
                raise HTTPException(status_code=404, detail="Student not found")

            result = session.execute(
                text(
                    "SELECT e.id AS enrollment_id, "
                    "       c.id AS course_id, "
                    "       c.name AS course_name, "
                    "       c.description AS course_description, "
                    "       e.enrolled_at "
                    "FROM enrollments e "
                    "JOIN courses c ON c.id = e.course_id "
                    "WHERE e.student_id = :student_id "
                    "ORDER BY e.id"
                ),
                {"student_id": student_id},
            )
            courses = [dict(row) for row in result.mappings()]
    except HTTPException:
        raise
    except Exception:
        # The real cause goes to the server log; the client gets the fixed
        # INTERNAL_ERROR_DETAIL and never the driver message.
        logger.exception("Error fetching courses")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)

    if not courses:
        return {
            "message": f"Student {student_id} is not enrolled in any course",
            "student_id": student_id,
            "count": 0,
        }
    return courses


@app.delete("/enroll/{enrollment_id}", status_code=204)
def delete_enrollment(enrollment_id: int):
    """Cancel one enrollment, releasing the seat but keeping the student.

    The enrollment row is identified by its own primary key, not by the
    student/course pair, so a student can hold several enrollments and cancel
    them one at a time.

    Args:
        enrollment_id: Primary key of the enrollment to remove. This is the id
            returned by POST /enroll, not the student or course id.

    Returns:
        None: A 204 No Content response, which carries no body, so there is no
            message to report. Returned only when a row was actually deleted.

    Raises:
        HTTPException: 404 if no enrollment has that id, or 500 if the delete
            fails.
    """

    try:
        with SessionLocal() as session:
            result = session.execute(
                text("DELETE FROM enrollments WHERE id = :id"),
                {"id": enrollment_id},
            )
            deleted = result.rowcount
            if deleted:
                session.commit()
    except Exception:
        # The real cause goes to the server log; the client gets the fixed
        # INTERNAL_ERROR_DETAIL and never the driver message.
        logger.exception("Error deleting enrollment")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)

    if not deleted:
        raise HTTPException(status_code=404, detail="Enrollment not found")
