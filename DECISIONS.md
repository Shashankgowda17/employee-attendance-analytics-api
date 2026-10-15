# DECISIONS.md

1. **Indexes.** A unique `employees.emp_code` index prevents duplicate employee identities. A unique `attendance_logs(emp_code, date)` index guarantees one daily record. `(emp_code, punch_in desc)` helps find the latest punch-in; `(date desc, emp_code asc)` supports attendance ordering; `(status, date)` helps status/date filters. `(department, joined_on)` and `joined_on` support department and headcount queries. I rejected indexing every field because unused indexes slow writes and consume storage.

2. **Punch-in race.** Concurrent requests may both pass an initial lookup, but the unique compound index allows only one insert. MongoDB raises `DuplicateKeyError` for the loser, which the API converts to 409. A pre-check alone is not a concurrency guarantee.

3. **Ties.** The leaderboard uses MongoDB competition rank and sorts ties by `emp_code`. Filtering by rank after ranking includes every employee tied at the limit cutoff, producing ranks such as 1, 2, 2, 4.

4. **Headcount.** Department summary starts with employees who joined by month end and looks up logs, preserving employees with no attendance records.

5. **100x data.** I would profile explain plans with realistic data and consider pre-aggregated monthly summaries to reduce repeated analytics work.
