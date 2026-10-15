# REVIEW.md

Defects found in the starter `app/main.py`, all fixed in my version.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Applies `shift_start` to the datetime's own (UTC/server) clock, not IST. | Shift 09:30 IST, punch 10:00 IST (04:30 UTC) gives a huge negative number, so 0 late. | Build the shift start as an IST datetime on the record's attendance date. |
| 2 | `compute_late_minutes` | Floors first, then tests `> 10`. | 09:40:01 on a 09:30 shift gives 0; R2 says 10. | Compare in seconds (`> 600`), then floor to minutes. |
| 3 | `compute_late_minutes`, `punch_in` | No overnight handling (R1); date is the plain calendar date. | Shift 22:00-06:00, punch 00:30: dated the 7th, should belong to the 6th. | `attendance_date()` shifts early-morning punches of overnight shifts to the previous day. |
| 4 | `compute_work_hours` | Python `round()` is banker's rounding on a float; no truncation to whole seconds. | 18 s of work gives 0.0, R4 wants 0.01; 09:40:00.900 is not truncated. | Integer seconds, `Decimal` half-up (hundredths = seconds/36). |
| 5 | `compute_overtime` | No 30-minute minimum (R3); ignores overnight shifts; compares a tz-aware IST value with whatever `punch_out` is, so a naive datetime raises `TypeError`. | 10 minutes after shift end returns 10. Overnight overtime is wrong or a 500. | Overtime only if floor-minutes >= 30; shift end moves to the next day for overnight shifts; everything is aware UTC. |
| 6 | module setup | `MongoClient()` returns naive datetimes; `datetime.now()` / `fromtimestamp()` use the server's local zone. | Different results on a server not in UTC; naive/aware arithmetic errors. | `tz_aware=True`; all instants are aware UTC and converted to epoch ms at the boundary. |
| 7 | `health` | Always returns ok without touching MongoDB. | Stop MongoDB: still 200. | Ping MongoDB; 503 on failure. |
| 8 | `create_employee` | Check-then-insert race; no unique index. | Two parallel POSTs both return 201 with a duplicate `emp_code`. | Unique index on `emp_code`; catch `DuplicateKeyError` -> 409. |
| 9 | `EmployeeIn` | No validation: `emp_code` format, email, shift pattern, `joined_on` date, `shift_start == shift_end`. `created_at` is a naive local datetime and is returned as a string, not epoch ms. | `joined_on: "2026-02-30"` or `shift_start == shift_end` gives 201. | Pydantic constraints and validators; `created_at` stored as UTC and returned as ms. |
| 10 | `list_employees` | `skip = page * page_size` (page 1 skips the first page); `total` ignores the filter; no sort; no bounds on `page` / `page_size`. | Page 1 is the second page; `?department=X` shows the wrong total; `page=0` gives a negative skip. | `(page-1)*size`, filtered count, sort by `emp_code`, `ge`/`le` bounds (422). |
| 11 | `punch_in` | Unknown employee: `emp` is `None`, so `emp["shift_start"]` raises `TypeError` (500). | Punch in with an unknown `emp_code`. | Explicit 404. |
| 12 | `punch_in` | `status` is not validated, so `ABSENT` or any string is accepted. | `{"status": "ABSENT"}` returns 201. | `Literal["PRESENT","WFH","ON_DUTY"]`. |
| 13 | `punch_in`, `PunchInIn` | `punched_at: Optional[int]` is lax: `"123"` and `1.0` are coerced, seconds-like values pass; `if body.punched_at` treats a value of `0` as "now". | `punched_at: 1783312500` (seconds) is accepted. | `StrictInt`, 1e11 <= x <= 4102444800000; `is not None` check. |
| 14 | `punch_in` | Check-then-insert race (`find_one` then `insert_one`). | Two simultaneous requests both get 201. | Unique index on `(emp_code, date)` and `insert_one`; the loser gets 409. |
| 15 | `punch_in` | Date comes from `fromtimestamp` in server-local time, not IST; sub-second part is kept. | Server in UTC: a punch at 00:30 IST gets the previous date. | Truncate to seconds, IST attendance date (R1). |
| 16 | `punch_in`, `list_attendance` | Response leaks the internal `_id` as `id`, returns datetimes instead of epoch ms, and omits defaults for legacy records. | `id` in the body; `punch_in` is an ISO string. | `record_out()` serializer: no `_id`, ms instants, defaults for missing `late_minutes`/`half_day`/`history`. |
| 17 | `list_attendance` | Loads every matching document, sorts and slices in Python; sort lacks the `emp_code` tie-break; `page`, `page_size`, dates and `status` unvalidated. | Slow and memory-heavy at 100k docs; inconsistent order between pages. | Server-side `sort` + `skip`/`limit`, `count_documents`, validation (422), hinted compound indexes. |
| 18 | whole file | No indexes are created. | `explain` shows COLLSCAN. | Idempotent index creation at startup (see DECISIONS.md). |

**Looked at and judged fine:** `load_dotenv()` does not override variables that are already set, so real environment variables win. Sync `def` endpoints run in FastAPI's thread pool and `MongoClient` is thread-safe, so I kept blocking PyMongo. Popping `_id` before returning was correct in intent; I replaced it with an explicit field whitelist.
