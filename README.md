# Course Enrollment API

A small FastAPI service where students enroll in courses, backed by PostgreSQL
(Neon). It is a standalone application: it builds its own database connection and
runs independently of any other API in the workspace.

```
student_course_mgmt.py   the application (routes, validation, SQL)
schema.sql               the table definitions, applied once by hand
.env                     DATABASE_URL and the ngrok key (git-ignored)
```

## Running it

```bash
pip install fastapi uvicorn sqlalchemy psycopg2-binary python-dotenv pydantic
uvicorn student_course_mgmt:app --reload
```

Interactive API docs are then at `http://127.0.0.1:8000/docs`.

The tables must exist before the app is useful. `schema.sql` is idempotent, so
applying it more than once is harmless:

```bash
psql "$DATABASE_URL" -f schema.sql
```

## The data model

```
students  (owned by the student-management API, read-only here)
    id, name, age, city, email, course

courses
    id          serial primary key
    name        varchar(100) not null unique
    description varchar(500)

enrollments
    id          serial primary key
    student_id  -> students.id   on delete cascade
    course_id   -> courses.id    on delete cascade
    enrolled_at timestamptz not null default now()
    unique (student_id, course_id)
```

Three decisions in that shape carry most of the logic:

**`unique (student_id, course_id)` is what actually prevents double enrollment.**
The handler also checks for an existing enrollment so it can return a readable
message, but that check is advisory. Two requests arriving at the same moment can
both read "not enrolled" and both proceed; only one INSERT can commit. The
constraint is the authority, and the losing request's `IntegrityError` is
translated into the same `409` the check would have produced. A read-then-write
check on its own would leave that race open.

**Both foreign keys cascade.** Deleting a student removes their enrollments;
deleting a course releases every seat in it. Neither leaves rows pointing at
something that no longer exists.

**The `students` table is not created here.** Its ids are read and referenced, so
a student that does not exist is a `404`, never an implicitly created student.

## Endpoints

| Method | Path | Success | Errors |
| --- | --- | --- | --- |
| `GET` | `/courses` | `200` list | `500` |
| `POST` | `/courses` | `201` created row | `409` name taken, `500` |
| `POST` | `/enroll` | `201` created enrollment | `404` student, `404` course, `409` already enrolled, `500` |
| `GET` | `/students/{student_id}/courses` | `200` list | `404` no such student, `500` |
| `DELETE` | `/enroll/{enrollment_id}` | `204` empty body | `404` no such enrollment, `500` |

Both create routes answer with `RETURNING *`, so the response contains the
generated `id`. That matters: enrolling needs a `course_id`, and cancelling needs
an `enrollment_id`, so a client could not act on a response that omitted them.

### Worked example

```bash
curl -X POST localhost:8000/courses \
  -H 'Content-Type: application/json' \
  -d '{"name": "Python", "description": "Basic Python"}'
# {"id":1,"name":"Python","description":"Basic Python"}

curl -X POST localhost:8000/enroll \
  -H 'Content-Type: application/json' \
  -d '{"student_id": 1, "course_id": 1}'
# {"id":1,"student_id":1,"course_id":1,"enrolled_at":"..."}

curl localhost:8000/enroll -X POST -H 'Content-Type: application/json' \
  -d '{"student_id": 1, "course_id": 1}'
# 409 {"detail":"Student is already enrolled in this course"}

curl localhost:8000/students/1/courses
# [{"enrollment_id":1,"course_id":1,"course_name":"Python",...}]

curl -X DELETE localhost:8000/enroll/1
# 204, empty body
```

## Empty results answer with a message, not `[]`

An empty read is a normal outcome, not a failure, so it stays `200` and returns
an explanatory object instead of a silent empty list:

```jsonc
// GET /courses with nothing in the catalogue
{"message": "No courses found. Add one with POST /courses.", "count": 0}

// GET /students/2/courses for a student holding no enrollments
{"message": "Student 2 is not enrolled in any course", "student_id": 2, "count": 0}
```

A `404` is kept strictly for "that thing does not exist". `GET
/students/999999/courses` returns `404 Student not found`, never the message
above — the student is looked up before the enrollment list is read, precisely
because a student with no enrollments and a student who does not exist both
produce an empty result set, and only one of them is a 404.

**Trade-off worth knowing:** these two routes now return a JSON array when there
is data and a JSON object when there is not. A client doing `response.json()[0]`
will break on the empty case, so consumers should check for the `message` key
first. The alternative — always returning `{"courses": [...], "count": n}` —
would keep one stable shape at the cost of changing the populated response too.

## Error handling

Every anticipated rejection returns a specific status and a message written for
a human: `404` for a missing student, course or enrollment; `409` for a duplicate
course name or a duplicate enrollment.

Unexpected failures return `500` with one fixed sentence:

```json
{"detail": "Something went wrong while processing the request."}
```

That text is deliberately generic. The underlying database exception names the
failing statement, the table names and the violated constraint, which is useful
in a log and is not something to hand to a caller. So each handler calls
`logger.exception(...)` — the real cause, with traceback, to the server log — and
returns `INTERNAL_ERROR_DETAIL` to the client:

```python
except Exception:
    # The real cause goes to the server log; the client gets the fixed
    # INTERNAL_ERROR_DETAIL and never the driver message.
    logger.exception("Error fetching courses")
    raise HTTPException(status_code=500, detail=INTERNAL_ERROR_DETAIL)
```

Those records are emitted at `ERROR` level on this module's logger, so they
propagate to whatever root handler is configured. Uvicorn configures one, so they
appear during a normal `uvicorn` run; in a bare script or a test run, add

```python
logging.basicConfig(level=logging.ERROR)
```

if nothing is printed.

## Validation

Payloads are Pydantic models, so malformed input is rejected with a `422` before
any query runs.

- `CourseCreate.name` — 2–100 characters, whitespace collapsed so `"  Data   Science "`
  and `"Data Science"` are the same course. Case is left alone, matching how the
  sibling student API treats its `course` column.
- `EnrollCreate.student_id` / `course_id` — integers of at least 1.

## Notes and limitations

- **This app creates nothing.** No `CREATE TABLE`, no `create_all()`, no Alembic.
  That is why `schema.sql` exists and why applying it is a manual step. A fresh
  database will fail every route until it is applied.
- **The startup query is a smoke test.** `get_courses()` is called once at import
  purely to surface a missing table early. The result is discarded and a failure
  is printed rather than raised, so a broken database cannot stop the server from
  booting.
- **Raw SQL, not the ORM.** Queries are parameterised via `text()` with bound
  parameters, matching the sibling student-management API. Values are never
  interpolated into SQL.
- **`sessions` are per-request** and closed by the `with` block.
- **The duplicate-name check on `POST /courses` is only the constraint.** Unlike
  enrollment there is no pre-read, because the database can arbitrate a name
  clash by itself and a read-then-write would merely add a race.
- **Course names are case-sensitive**, so `Python` and `python` are two courses.
- **`students.course`** is a free-text column in the student-management API and
  is unrelated to the `enrollments` table. Nothing here keeps the two in sync.
