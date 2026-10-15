# REVIEW.md

I reviewed the original starter `app/main.py` against the contract and recorded the defects below. The fixes are in the submitted implementation.

| # | Where (starter code) | What is wrong | How to notice (test/input/symptom) | How it was fixed |
|---|---|---|---|---|
| 1 | `health()` | Always returns 200 without checking MongoDB. | Stop MongoDB and call `/health`; it still reports `ok`. | Ping MongoDB and return 503 on connection failure. |
| 2 | `create_employee()` | Check-then-insert allows concurrent duplicate `emp_code` requests. | Send two simultaneous creates with the same code; both can pass the initial lookup. | Create a unique database index and translate `DuplicateKeyError` to 409. |
| 3 | `create_employee()` / `EmployeeIn` | No input validation for employee code, email, dates, shift times, field lengths, or equal shift boundaries. | Submit `emp_code="bad"`, malformed email/date/time, or equal shift times. | Add explicit Pydantic field/model validators. |
| 4 | `list_employees()` | `skip = page * page_size` skips the first page; total ignores department filter; no deterministic sort or bounds. | Page 1 starts after the first 20 rows; filtered total differs from returned set. | Use `(page - 1) * page_size`, filtered `count_documents`, sorted query, and page-size bounds. |
| 5 | `punch_in()` | Does not return 404 for an unknown employee before accessing shift fields. | Punch in with an unregistered code causes a server error. | Check employee existence and return 404. |
| 6 | `punch_in()` | Uses local machine time and calendar date instead of epoch-ms UTC conversion and IST attendance date. | Run on a non-IST machine or submit a punch near midnight UTC/IST. | Convert epoch milliseconds to UTC, truncate to seconds, and derive date in IST. |
| 7 | `punch_in()` / `compute_late_minutes()` | Late helper resets the punch timestamp's hour on the same date and ignores the correct shift date/overnight handling. | Test an overnight shift or a punch around midnight. | Calculate against the shift start instant on the resolved attendance date. |
| 8 | `punch_in()` | No validation of punch status or timestamp type; truthiness treats timestamp `0` as omitted. | Send invalid status or `punched_at: 0`; behavior is incorrect/inconsistent. | Strict integer timestamp validation, explicit `is not None`, and allowed-status validation. |
| 9 | `punch_in()` | Duplicate protection relies only on a lookup, with no unique `(emp_code, date)` index. | Parallel punch-ins can create duplicate attendance records. | Add a unique compound index and return 409 on duplicate-key races. |
| 10 | `compute_work_hours()` | Python `round()` uses ties-to-even rather than required half-up rounding. | Test a duration whose hours round exactly at a half-cent boundary. | Use `Decimal` with `ROUND_HALF_UP` after whole-second truncation. |
| 11 | `compute_overtime()` | Does not apply the 30-minute minimum and does not handle overnight shift end on the next day. | Test 29:59 overtime or a 22:00–06:00 shift. | Resolve shift end on the correct calendar day and count only if floored minutes are at least 30. |
| 12 | `list_attendance()` | Loads every matching document into Python, sorts/slices in memory, and counts only the fetched list. | Test a large collection; memory and latency grow with the result set. | Use indexed MongoDB sort, skip/limit, and `count_documents` with the same filters. |
| 13 | `list_attendance()` | Missing date/status validation, pagination bounds, and tie-break sort by `emp_code`. | Reverse date range, invalid status, oversized page size, or same-date records produce incorrect behavior. | Validate filters and paginate using contract-compliant MongoDB query/sort. |
| 14 | API timestamp boundaries | Starter returns BSON datetimes/ISO strings rather than epoch milliseconds and does not consistently truncate instants to whole seconds. | Inspect `created_at`, `punch_in`, and `punch_out` response types. | Convert API instants to epoch milliseconds and store truncated UTC BSON datetimes. |
| 15 | Missing endpoints | Punch-out, regularization, monthly analytics, department summary, leaderboard, trend, and explain were not implemented. | Requests to those documented routes return 404. | Implement all required routes, aggregation pipelines, concurrency guards, and explain support. |

## Inspected and not treated as defects

- Loading `.env` with `load_dotenv()` does not override real environment variables by default, so injected `MONGO_URI` and `MONGO_DB` values retain priority.
- The employee business identifier is `emp_code`; MongoDB `_id` should remain internal and is not used as the public identity.
- The default page size of 20 in the starter matches the contract; the defects were the skip calculation, missing bounds, count/filter mismatch, and absent deterministic ordering.
