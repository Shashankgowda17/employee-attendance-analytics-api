"""Employee Attendance & Analytics API.

Run from the project root with: python -m uvicorn app.main:app --port 8000
Environment: MONGO_URI (default mongodb://localhost:27017), MONGO_DB (default attendance_db)
"""
from __future__ import annotations

import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=True, tzinfo=timezone.utc)
db = client[MONGO_DB]
employees = db["employees"]
attendance = db["attendance_logs"]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENT_STATUSES | {"ABSENT", "LEAVE"}
EMP_RE = re.compile(r"^EMP\d{4,6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ------------------------------- validation ---------------------------------
def bad_request(message: str, status_code: int = 422) -> None:
    raise HTTPException(status_code=status_code, detail=message)


def parse_date(value: str, field: str = "date") -> date:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        bad_request(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        bad_request(f"{field} must be a valid date")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        bad_request("month must be YYYY-MM")
    y, m = map(int, value.split("-"))
    return date(y, m, 1), date(y, m, monthrange(y, m)[1])


def parse_time(value: str, field: str) -> str:
    if not TIME_RE.fullmatch(value):
        bad_request(f"{field} must be a valid HH:MM time")
    return value


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def epoch_ms_to_utc(value: int) -> datetime:
    try:
        # API instants are epoch milliseconds and are truncated to whole seconds.
        seconds = value // 1000
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (ValueError, OverflowError, OSError, TypeError):
        bad_request("timestamp must be a valid epoch-millisecond instant")


def now_utc_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def as_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(as_utc(value).timestamp()) * 1000


def local_dt(value: datetime) -> datetime:
    return as_utc(value).astimezone(IST)


def shift_end_datetime(record_date: str, shift_start: str, shift_end: str) -> datetime:
    day = parse_date(record_date)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    end_day = day + timedelta(days=1) if (eh, em) <= (sh, sm) else day
    return datetime.combine(end_day, time(eh, em), tzinfo=IST).astimezone(UTC)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = local_dt(punch_in)
    sh = tuple(map(int, shift_start.split(":")))
    eh = tuple(map(int, shift_end.split(":")))
    day = local.date()
    # For overnight shifts, early-morning punches belong to the shift that began yesterday.
    if eh <= sh and (local.hour, local.minute) < eh:
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, record_date: str) -> int:
    day = parse_date(record_date)
    h, m = map(int, shift_start.split(":"))
    start = datetime.combine(day, time(h, m), tzinfo=IST).astimezone(UTC)
    elapsed = int((as_utc(punch_in) - start).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((as_utc(punch_out) - as_utc(punch_in)).total_seconds()))
    return round_half_up(seconds / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, record_date: str) -> int:
    end = shift_end_datetime(record_date, shift_start, shift_end)
    minutes = int((as_utc(punch_out) - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def derive_attendance(emp: dict, record_date: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    if punch_in is None or punch_out is None:
        return {"work_hours": None, "late_minutes": 0 if punch_in is None else compute_late_minutes(punch_in, emp["shift_start"], record_date),
                "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {"work_hours": hours,
            "late_minutes": compute_late_minutes(punch_in, emp["shift_start"], record_date),
            "overtime_minutes": compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], record_date),
            "half_day": hours < 4.50}


def api_value(value: Any) -> Any:
    """Convert Mongo values into contract-friendly JSON values (instants as epoch ms)."""
    if isinstance(value, datetime):
        return as_epoch_ms(value)
    if isinstance(value, dict):
        return {k: api_value(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [api_value(v) for v in value]
    return value


def api_attendance(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    result["id"] = str(doc.get("_id", doc.get("id", "")))
    result["punch_in"] = as_epoch_ms(result.get("punch_in"))
    result["punch_out"] = as_epoch_ms(result.get("punch_out"))
    result["history"] = api_value(result.get("history", []))
    result.setdefault("half_day", False)
    return result


def api_employee(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    # created_at is an API instant, so return epoch milliseconds just like punch timestamps.
    if isinstance(result.get("created_at"), datetime):
        result["created_at"] = as_epoch_ms(result["created_at"])
    return result


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def page_params(page: int, page_size: int) -> tuple[int, int]:
    if page < 1:
        bad_request("page must be >= 1")
    if page_size < 1 or page_size > 100:
        bad_request("page_size must be between 1 and 100")
    return (page - 1) * page_size, page_size


def ensure_indexes() -> None:
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employees_department_joined")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
    attendance.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_emp_punchin")
    attendance.create_index([("status", ASCENDING), ("date", ASCENDING)], name="ix_attendance_status_date")


@app.on_event("startup")
def startup() -> None:
    # Idempotent; duplicate-key indexes intentionally fail startup if existing data violates them.
    ensure_indexes()


# ---------------------------------- models -----------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def valid_emp_code(cls, value: str) -> str:
        if not EMP_RE.fullmatch(value):
            raise ValueError("emp_code must match EMP followed by 4 to 6 digits")
        return value

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if not EMAIL_RE.fullmatch(value):
            raise ValueError("email must be valid")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift_time(cls, value: str) -> str:
        if not TIME_RE.fullmatch(value):
            raise ValueError("shift time must be HH:MM")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        parse_date(value, "joined_on")
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        if value not in PRESENT_STATUSES:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in ALL_STATUSES:
            raise ValueError("status must be PRESENT, WFH, ON_DUTY, ABSENT, or LEAVE")
        return value


# -------------------------------- endpoints ----------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_seconds().replace(tzinfo=None)
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return api_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    skip, size = page_params(page, page_size)
    query = {"department": department} if department else {}
    total = employees.count_documents(query)
    items = list(employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(size))
    return {"items": items, "total": total, "page": page, "page_size": size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    instant = epoch_ms_to_utc(body.punched_at) if body.punched_at is not None else now_utc_seconds()
    record_date = attendance_date_for(instant, emp["shift_start"], emp["shift_end"])
    doc = {"emp_code": body.emp_code, "date": record_date, "status": body.status,
           "punch_in": instant.replace(tzinfo=None), "punch_out": None, "work_hours": None,
           "late_minutes": compute_late_minutes(instant, emp["shift_start"], record_date),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        result = attendance.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    doc["_id"] = result.inserted_id
    return api_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    instant = epoch_ms_to_utc(body.punched_at) if body.punched_at is not None else now_utc_seconds()
    # The latest still-open punch-in at or before the requested instant wins.
    record = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": instant.replace(tzinfo=None)}},
                                 sort=[("punch_in", DESCENDING)])
    if not record:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")
    punch_in_dt = as_utc(record["punch_in"])
    if instant <= punch_in_dt or instant > punch_in_dt + timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")
    derived = derive_attendance(emp, record["date"], punch_in_dt, instant)
    updated = attendance.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": instant.replace(tzinfo=None), **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return api_attendance(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[str] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    skip, size = page_params(page, page_size)
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        bad_request("date_from must be <= date_to")
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        if status not in ALL_STATUSES:
            bad_request("invalid status")
        query["status"] = status
    total = attendance.count_documents(query)
    docs = list(attendance.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(size))
    return {"items": [api_attendance(d) for d in docs], "total": total, "page": page, "page_size": size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    record_date = date
    parse_date(record_date, "date")
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    old = attendance.find_one({"emp_code": emp_code, "date": record_date})
    if not old:
        raise HTTPException(status_code=404, detail="attendance record not found")

    old_status = old.get("status", "PRESENT")
    old_in = as_utc(old["punch_in"]) if old.get("punch_in") is not None else None
    old_out = as_utc(old["punch_out"]) if old.get("punch_out") is not None else None
    new_status = body.status if body.status is not None else old_status
    new_in = epoch_ms_to_utc(body.punch_in) if body.punch_in is not None else old_in
    new_out = epoch_ms_to_utc(body.punch_out) if body.punch_out is not None else old_out

    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            bad_request("punch times cannot be supplied with ABSENT or LEAVE")
        new_in = new_out = None
    else:
        if new_in is None:
            bad_request("presence status requires punch_in")
        if local_dt(new_in).date().isoformat() != record_date:
            # For overnight shifts, a post-midnight punch may belong to the previous shift date.
            expected_date = attendance_date_for(new_in, emp["shift_start"], emp["shift_end"])
            if expected_date != record_date:
                bad_request("punch_in must belong to the attendance date")
        if new_out is not None:
            if new_out <= new_in or new_out > new_in + timedelta(hours=24):
                bad_request("punch_out must be after punch_in and within 24 hours")

    changes: dict[str, dict[str, Any]] = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_in != old_in:
        changes["punch_in"] = {"from": as_epoch_ms(old_in), "to": as_epoch_ms(new_in)}
    if new_out != old_out:
        changes["punch_out"] = {"from": as_epoch_ms(old_out), "to": as_epoch_ms(new_out)}
    if not changes:
        bad_request("request does not change the record")

    derived = derive_attendance(emp, record_date, new_in, new_out) if new_status in PRESENT_STATUSES else {
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    for key, new_value in derived.items():
        old_value = old.get(key, False if key == "half_day" else None if key == "work_hours" else 0)
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}

    history_item = {"at": now_utc_seconds().replace(tzinfo=None), "by": body.regularized_by,
                    "reason": body.reason, "changes": changes}
    set_values = {"status": new_status, "punch_in": new_in.replace(tzinfo=None) if new_in else None,
                  "punch_out": new_out.replace(tzinfo=None) if new_out else None, **derived}
    # Optimistic compare-and-set prevents silently overwriting concurrent regularizations.
    filter_doc = {"_id": old["_id"], "status": old.get("status", "PRESENT"),
                  "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out")}
    updated = attendance.find_one_and_update(filter_doc, {"$set": set_values, "$push": {"history": history_item}},
                                             return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance changed concurrently; retry")
    return api_attendance(updated)


# ------------------------------- analytics ----------------------------------
def employee_monthly_pipeline(emp_code: str, start: date, end: date) -> list[dict]:
    start_s, end_s = start.isoformat(), end.isoformat()
    weekday = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_s, "$lte": end_s}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$status", list(PRESENT_STATUSES)]},
                {"$gte": [weekday, 2]}, {"$lte": [weekday, 6]}]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]


def monthly_employee_metrics(emp: dict, month: str) -> dict:
    start, end = parse_month(month)
    join_date = parse_date(emp["joined_on"], "joined_on")
    first_day = max(start, join_date)
    days = working_days(first_day, end) if first_day <= end else 0
    records = list(attendance.aggregate(employee_monthly_pipeline(emp["emp_code"], start, end)))
    agg = records[0] if records else {}
    present = float(agg.get("present_days", 0))
    return {"emp_code": emp["emp_code"], "name": emp["name"], "department": emp["department"], "month": month,
            "working_days": days, "present_days": round_half_up(present, 2), "leave_days": int(agg.get("leave_days", 0)),
            "late_count": int(agg.get("late_count", 0)), "total_late_minutes": int(agg.get("total_late_minutes", 0)),
            "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
            "attendance_pct": None if days == 0 else round_half_up(Decimal(str(present)) / Decimal(days) * 100, 2)}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    parse_month(month)
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    return monthly_employee_metrics(emp, month)


def department_summary_pipeline(start: date, end: date, department: Optional[str] = None) -> list[dict]:
    employee_match: dict[str, Any] = {"joined_on": {"$lte": end.isoformat()}}
    if department:
        employee_match["department"] = department
    present_statuses = list(PRESENT_STATUSES)
    return [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]},
                {"$gte": ["$date", start.isoformat()]},
                {"$lte": ["$date", end.isoformat()]}
            ]}}},
            {"$project": {"date": 1, "status": 1, "half_day": 1, "late_minutes": 1,
                          "overtime_minutes": 1, "work_hours": 1}}
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$set": {"_weekday": {"$cond": [
            {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
            {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 0]}}},
        {"$group": {"_id": "$emp_code", "department": {"$first": "$department"},
            "present_days": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$logs.status", present_statuses]}, {"$gte": ["$_weekday", 2]}, {"$lte": ["$_weekday", 6]}]},
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$logs.status", present_statuses]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$logs.status", present_statuses]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, 1, 0]}}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"}, "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"}, "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"}, "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_count": {"$sum": "$work_hours_count"}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]},
                {"$divide": ["$work_hours_sum", "$work_hours_count"]}, None]}}},
        {"$sort": {"department": 1}}
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = parse_month(month)
    rows = list(employees.aggregate(department_summary_pipeline(start, end, department)))
    items = []
    for row in rows:
        items.append({"department": row["department"], "month": month, "headcount": int(row["headcount"]),
            "present_days": round_half_up(row.get("present_days", 0), 2),
            "avg_work_hours": None if row.get("avg_work_hours") is None else round_half_up(row["avg_work_hours"], 2),
            "late_count": int(row.get("late_count", 0)), "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count": int(row.get("leave_count", 0)), "on_duty_count": int(row.get("on_duty_count", 0))})
    return {"month": month, "items": items}

def late_leaderboard_pipeline(start: date, end: date, limit: int = 10, department: Optional[str] = None) -> list[dict]:
    employee_match: dict[str, Any] = {"joined_on": {"$lte": end.isoformat()}}
    if department:
        employee_match["department"] = department
    return [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.joined_on": {"$lte": end.isoformat()}, **({"employee.department": department} if department else {})}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department",
                       "total_late_minutes": 1, "rank": 1}}
    ]


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = parse_month(month)
    rows = list(attendance.aggregate(late_leaderboard_pipeline(start, end, limit, department)))
    return {"month": month, "items": [{"emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]), "rank": int(r["rank"])} for r in rows]}

def department_trend_pipeline(department: str, start: date, end: date) -> list[dict]:
    start_dt = datetime.combine(start, time.min, tzinfo=UTC).replace(tzinfo=None)
    end_exclusive = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC).replace(tzinfo=None)
    return [
        {"$match": {"department": department}}, {"$limit": 1},
        {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": start.isoformat()}},
                      "present_count": {"$literal": 0}, "late_count": {"$literal": 0}}},
        {"$unionWith": {"coll": "attendance_logs", "pipeline": [
            {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"}, {"$match": {"emp.department": department}},
            {"$addFields": {"day": {"$dateFromString": {"dateString": "$date"}}}},
            {"$group": {"_id": "$day",
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT_STATUSES)]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
            {"$project": {"_id": 0, "day": "$_id", "present_count": 1, "late_count": 1}}
        ]}},
        {"$group": {"_id": "$day", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$project": {"_id": 0, "day": "$_id", "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_exclusive]}}},
        {"$set": {"present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]},
            "day_str": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}}, "weekday": {"$dayOfWeek": "$day"}}},
        {"$lookup": {"from": "employees", "let": {"d": "$day_str"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}]}}},
            {"$count": "count"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
            "is_working_day": {"$and": [{"$ne": ["$weekday", 1]}, {"$ne": ["$weekday", 7]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$eq": ["$attendance_rate", None]}, None,
            {"$divide": [{"$floor": {"$add": [{"$multiply": ["$attendance_rate", 10000]}, 0.5]}}, 10000]}]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": "$day_str", "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_raw": 1}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(alias="from"), to_date: str = Query(alias="to")):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")
    if end < start:
        bad_request("to must be >= from")
    if (end - start).days > 91:
        bad_request("date range must not exceed 92 days")
    if not employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="department not found")
    rows = list(employees.aggregate(department_trend_pipeline(department, start, end)))
    output = []
    for row in rows:
        rate, moving = row.get("attendance_rate"), row.get("moving_avg_raw")
        output.append({"date": row["date"], "is_working_day": bool(row["is_working_day"]), "headcount": int(row["headcount"]),
            "present_count": round_half_up(row["present_count"], 2), "late_count": int(row["late_count"]),
            "attendance_rate": None if rate is None else round_half_up(rate, 4),
            "moving_avg_7d": None if moving is None else round_half_up(moving, 4)})
    return {"department": department, "from": from_date, "to": to_date, "items": output}


# ----------------------------- query explain --------------------------------
def explain_find(collection, query: dict, sort: list[tuple[str, int]], skip: int = 0, limit: int = 20):
    # Explain the same find/sort/pagination operation used by the endpoint.
    command = {"find": collection.name, "filter": query, "sort": dict(sort), "skip": skip, "limit": limit}
    return db.command({"explain": command, "verbosity": "executionStats"})


def explain_aggregate(collection, pipeline: list[dict]):
    return db.command({"explain": {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}},
                       "verbosity": "executionStats"})


@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                  department: Optional[str] = None, date_from: Optional[str] = None,
                  date_to: Optional[str] = None, from_date: Optional[str] = Query(None, alias="from"),
                  to_date: Optional[str] = Query(None, alias="to"), page: int = Query(1, ge=1),
                  page_size: int = Query(20, ge=1, le=100), limit: int = Query(10, ge=1, le=50),
                  status: Optional[str] = None):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        bad_request(f"endpoint must be one of: {', '.join(sorted(allowed))}")
    skip, size = page_params(page, page_size)
    if endpoint == "attendance_list":
        q: dict[str, Any] = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from: parse_date(date_from, "date_from")
        if date_to: parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to: bad_request("date_from must be <= date_to")
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status:
            if status not in ALL_STATUSES: bad_request("invalid status")
            q["status"] = status
        return explain_find(attendance, q, [("date", DESCENDING), ("emp_code", ASCENDING)], skip, size)
    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            bad_request("department, from, and to are required")
        start_d, end_d = parse_date(from_date, "from"), parse_date(to_date, "to")
        if end_d < start_d or (end_d - start_d).days > 91:
            bad_request("invalid trend date range (maximum 92 days)")
        if not employees.find_one({"department": department}):
            raise HTTPException(status_code=404, detail="department not found")
        return explain_aggregate(employees, department_trend_pipeline(department, start_d, end_d))
    if not month:
        bad_request("month is required for this explain endpoint")
    start_d, end_d = parse_month(month)
    if endpoint == "employee_monthly":
        if not emp_code: bad_request("emp_code is required")
        if not employees.find_one({"emp_code": emp_code}): raise HTTPException(status_code=404, detail="employee not found")
        return explain_aggregate(attendance, employee_monthly_pipeline(emp_code, start_d, end_d))
    if endpoint == "department_summary":
        return explain_aggregate(employees, department_summary_pipeline(start_d, end_d, department))
    return explain_aggregate(attendance, late_leaderboard_pipeline(start_d, end_d, limit, department))
