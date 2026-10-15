"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded; real environment variables win)

Layout of this file: config -> time/rule helpers -> models -> serializers -> index setup
-> query/pipeline builders -> endpoints.  All instants are stored as tz-aware UTC datetimes
and exposed as integer epoch milliseconds; calendar values are plain strings (IST).
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment

log = logging.getLogger("attendance")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,  # datetimes come back as aware UTC, never naive
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

EPOCH_MIN = 100_000_000_000  # below this a value looks like seconds
EPOCH_MAX = 4_102_444_800_000
EpochMs = Annotated[StrictInt, Field(ge=EPOCH_MIN, le=EPOCH_MAX)]  # strict: no floats/strings/bools

MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# Index names are explicit because the queries below hint them (see DECISIONS.md).
IX_EMP_UNIQUE = "emp_code_1"
IX_EMP_DEPT = "department_1_emp_code_1"
IX_EMP_JOINED = "joined_on_1"
IX_EMP_DEPT_JOINED = "department_1_joined_on_1"
IX_LOG_UNIQUE = "emp_code_1_date_1"
IX_LOG_DATE = "date_-1_emp_code_1"
IX_LOG_STATUS = "status_1_date_-1_emp_code_1"
IX_LOG_PUNCH = "emp_code_1_punch_in_-1"

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the same index already exists."""
    global _indexes_ready
    specs = [
        (employees, [("emp_code", 1)], IX_EMP_UNIQUE, True),
        (employees, [("department", 1), ("emp_code", 1)], IX_EMP_DEPT, False),
        (employees, [("joined_on", 1)], IX_EMP_JOINED, False),
        (employees, [("department", 1), ("joined_on", 1)], IX_EMP_DEPT_JOINED, False),
        (logs, [("emp_code", 1), ("date", 1)], IX_LOG_UNIQUE, True),
        (logs, [("date", -1), ("emp_code", 1)], IX_LOG_DATE, False),
        (logs, [("status", 1), ("date", -1), ("emp_code", 1)], IX_LOG_STATUS, False),
        (logs, [("emp_code", 1), ("punch_in", -1)], IX_LOG_PUNCH, False),
    ]
    ok = True
    for coll, keys, name, unique in specs:
        try:
            coll.create_index(keys, name=name, unique=unique)
        except PyMongoError as exc:  # keep the app up; /health retries
            ok = False
            log.error("index %s failed: %s", name, exc)
    _indexes_ready = ok


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Time helpers (instants <-> epoch ms, IST calendar logic)
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)  # exact, no float division


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def dt_to_ms(dt: datetime) -> int:
    delta = as_utc(dt) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def truncate_seconds(dt: datetime) -> datetime:
    return as_utc(dt).replace(microsecond=0)  # R1: whole seconds before any calculation


def now_utc() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)  # Mongo keeps milliseconds


def ms_or_none(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else dt_to_ms(dt)


def at_ist(d: date, hhmm: str, plus_days: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, int(hhmm[:2]), int(hhmm[3:]), tzinfo=IST) + timedelta(days=plus_days)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM strings compare correctly


def parse_ymd(value: str, name: str = "date") -> date:
    if not DATE_RE.match(value):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return date(int(value[:4]), int(value[5:7]), int(value[8:]))
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid calendar date")


# --------------------------------------------------------------------------- #
# Business rules R1-R5
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; early-morning punches of an overnight shift
    belong to the shift that started the previous day."""
    ist = as_utc(punch_in).astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end) and ist.time() < time(int(shift_end[:2]), int(shift_end[3:])):
        d -= timedelta(days=1)
    return d.isoformat()


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then floor minutes since start."""
    start = at_ist(date.fromisoformat(date_str), shift_start)
    secs = int((truncate_seconds(punch_in) - start).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds/3600 rounded half-up to 2 decimals (hundredths = seconds/36)."""
    secs = int((truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds())
    hundredths = int((Decimal(secs) / Decimal(36)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return hundredths / 100


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift end (next day for overnight shifts); counted only if >= 30."""
    end = at_ist(date.fromisoformat(date_str), shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    mins = int((truncate_seconds(punch_out) - end).total_seconds()) // 60
    return mins if mins >= 30 else 0


def derive_fields(emp: dict, date_str: str, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All stored derived values for a record (DATA_MODEL.md section 3 table)."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, date_str, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(punch_out, date_str, emp["shift_start"], emp["shift_end"])
        out["half_day"] = wh < 4.5  # R5: the rounded value
    return out


def check_punch_span(punch_in: datetime, punch_out: datetime) -> None:
    delta = truncate_seconds(punch_out) - truncate_seconds(punch_in)
    if delta <= timedelta(0):
        raise HTTPException(422, "punch_out must be after punch_in")
    if delta > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")


def round_half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_RE)
    shift_end: str = Field("18:30", pattern=HHMM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date(int(v[:4]), int(v[5:7]), int(v[8:]))  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serializers (Mongo document -> API JSON; never exposes _id)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": ms_or_none(doc.get("created_at")),
    }


def _history_value(field: str, value: Any) -> Any:
    return ms_or_none(value) if field in ("punch_in", "punch_out") else value


def history_out(entry: dict) -> dict:
    return {
        "at": ms_or_none(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            f: {"from": _history_value(f, c.get("from")), "to": _history_value(f, c.get("to"))}
            for f, c in (entry.get("changes") or {}).items()
        },
    }


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": ms_or_none(doc.get("punch_in")),
        "punch_out": ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def get_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Query / pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def month_bounds(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        raise HTTPException(422, "invalid month")
    first = date(year, mon, 1)
    nxt = date(year + (mon == 12), mon % 12 + 1, 1) if year < 9999 else date(9999, 12, 31) + timedelta(days=1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def is_weekday_expr() -> dict:
    # $dayOfWeek: 1 = Sunday ... 7 = Saturday; the date string is a pure calendar date (UTC midnight).
    return {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": None, "onNull": None}}}, [2, 3, 4, 5, 6]]}


def present_weight_expr(weekday_only: bool) -> dict:
    """R5-R7: 1 per present record, 0.5 for a half day, 0 for ABSENT/LEAVE (optionally Mon-Fri only)."""
    cond: dict = {"$in": ["$status", PRESENCE]}
    if weekday_only:
        cond = {"$and": [cond, is_weekday_expr()]}
    return {"$cond": [cond, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE_MIN, 0]}, 1, 0]}


def attendance_list_query(emp_code, date_from, date_to, status, page, page_size) -> dict:
    f: dict = {}
    if emp_code:
        f["emp_code"] = emp_code
    start = parse_ymd(date_from, "date_from") if date_from else None
    end = parse_ymd(date_to, "date_to") if date_to else None
    if start and end and start > end:
        raise HTTPException(422, "date_from must not be after date_to")
    if start or end:
        f["date"] = {}
        if start:
            f["date"]["$gte"] = start.isoformat()
        if end:
            f["date"]["$lte"] = end.isoformat()
    if status:
        f["status"] = status
    hint = IX_LOG_UNIQUE if emp_code else IX_LOG_STATUS if status else IX_LOG_DATE
    return {"filter": f, "sort": [("date", -1), ("emp_code", 1)], "skip": (page - 1) * page_size, "limit": page_size, "hint": hint}


def monthly_pipeline(emp_code: str, month: str) -> tuple[str, list, str]:
    start, end = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_weight_expr(True)},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": IS_LATE},
            "total_late_minutes": {"$sum": LATE_MIN},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    return "attendance_logs", pipeline, IX_LOG_UNIQUE


def department_summary_pipeline(month: str, department: Optional[str]) -> tuple[str, list, str]:
    """Starts from employees (so people with zero logs are counted), joins each one's month of logs
    and reduces them to one stats document inside the $lookup, then groups by department."""
    start, end = month_bounds(month)
    emp_match: dict = {"joined_on": {"$lte": end}}  # R9
    if department:
        emp_match["department"] = department
    present = {"$in": ["$status", PRESENCE]}
    has_hours = {"$and": [present, {"$isNumber": "$work_hours"}]}
    stats = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_weight_expr(True)},
            "wh_cents": {"$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
            "wh_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
            "late_count": {"$sum": IS_LATE},
            "total_late_minutes": {"$sum": LATE_MIN},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    zero = lambda f: {"$ifNull": [f"$s.{f}", 0]}  # noqa: E731  (employee with no logs -> no stats doc)
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "pipeline": stats, "as": "s"}},
        {"$set": {"s": {"$ifNull": [{"$arrayElemAt": ["$s", 0]}, {}]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": zero("present_days")},
            "wh_cents": {"$sum": zero("wh_cents")},
            "wh_count": {"$sum": zero("wh_count")},
            "late_count": {"$sum": zero("late_count")},
            "total_late_minutes": {"$sum": zero("total_late_minutes")},
            "leave_count": {"$sum": zero("leave_count")},
            "on_duty_count": {"$sum": zero("on_duty_count")},
        }},
        {"$sort": {"_id": 1}},
    ]
    return "employees", pipeline, IX_EMP_DEPT_JOINED if department else IX_EMP_JOINED


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> tuple[str, list, str]:
    start, end = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped
    ]
    if department:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},  # 1,2,2,4
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return "attendance_logs", pipeline, IX_LOG_DATE


def trend_range(date_from: str, date_to: str) -> tuple[date, date]:
    start, end = parse_ymd(date_from, "from"), parse_ymd(date_to, "to")
    if end < start:
        raise HTTPException(422, "to must not be before from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    return start, end


def department_trend_pipeline(department: str, start: date, end: date) -> tuple[str, list, str]:
    codes = [e["emp_code"] for e in employees.find({"department": department}, {"emp_code": 1, "_id": 0})]
    if not codes:
        raise HTTPException(404, "department not found")
    lower = datetime(start.year, start.month, start.day, tzinfo=UTC)
    upper = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)  # bounds are [lower, upper)
    window = {"documents": [-6, 0]}  # current row + 6 preceding rows, clipped at the range start
    pipeline = [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {"_id": "$date", "present": {"$sum": present_weight_expr(False)}, "late": {"$sum": IS_LATE}}},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},  # one row per day
        {"$set": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
            "present": {"$ifNull": ["$present", 0]},
            "late": {"$ifNull": ["$late", 0]},
            "wd": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]},
        }},
        {"$lookup": {  # R9: employees of the department who joined on or before that day
            "from": "employees", "let": {"day": "$date"}, "as": "hc",
            "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}}, {"$count": "n"}],
        }},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        # rate in 1/10000 units, half-up using integer maths: floor((present*20000 + hc) / (2*hc))
        {"$set": {"rate_i": {"$cond": [
            {"$and": ["$wd", {"$gt": ["$headcount", 0]}]},
            {"$trunc": [{"$divide": [{"$add": [{"$multiply": ["$present", 20000]}, "$headcount"]}, {"$multiply": ["$headcount", 2]}]}]},
            None]}}},
        {"$set": {"has": {"$cond": [{"$ne": ["$rate_i", None]}, 1, 0]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "s": {"$sum": "$rate_i", "window": window},
            "c": {"$sum": "$has", "window": window},
        }}},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": "$wd", "headcount": 1,
            "present_count": "$present", "late_count": "$late",
            "attendance_rate": {"$cond": [{"$ne": ["$rate_i", None]}, {"$divide": ["$rate_i", 10000]}, None]},
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$c", 0]}, None,
                {"$divide": [{"$trunc": [{"$divide": [{"$add": [{"$multiply": ["$s", 2]}, "$c"]}, {"$multiply": ["$c", 2]}]}]}, 10000]}]},
        }},
    ]
    return "attendance_logs", pipeline, IX_LOG_UNIQUE


# --------------------------------------------------------------------------- #
# System / employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # uniqueness is enforced by the unique index, not by a prior read
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    f = {"department": department} if department else {}
    hint = IX_EMP_DEPT if department else IX_EMP_UNIQUE
    total = employees.count_documents(f, hint=hint)
    cur = employees.find(f).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size).hint(hint)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = truncate_seconds(ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc())
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(emp, d, body.status, ts, None),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = truncate_seconds(ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc())
    doc = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)], hint=IX_LOG_PUNCH)
    if doc is None:
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    check_punch_span(doc["punch_in"], ts)
    derived = derive_fields(emp, doc["date"], doc["status"], doc["punch_in"], ts)
    sets = {"punch_out": ts, **derived}
    # The filter makes the update a compare-and-set: only one concurrent punch-out can match.
    res = logs.update_one({"_id": doc["_id"], "punch_out": None}, {"$set": sets})
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    return record_out({**doc, **sets})


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
    total = logs.count_documents(q["filter"], hint=q["hint"])
    cur = logs.find(q["filter"]).sort(q["sort"]).skip(q["skip"]).limit(q["limit"]).hint(q["hint"])
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


def _changed(a: Any, b: Any) -> bool:
    if isinstance(a, datetime) or isinstance(b, datetime):
        return (None if a is None else dt_to_ms(a)) != (None if b is None else dt_to_ms(b))
    return a != b


def _no_history_guard(history: list) -> dict:
    if history:
        return {"history": {"$size": len(history)}}
    return {"$or": [{"history": {"$exists": False}}, {"history": {"$size": 0}}]}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    d = parse_ymd(date, "date").isoformat()
    emp = get_employee(emp_code)
    for _ in range(5):  # optimistic concurrency: retry if somebody else changed the record meanwhile
        doc = logs.find_one({"emp_code": emp_code, "date": d})
        if doc is None:
            raise HTTPException(404, "no record for that date")
        cur_status, cur_in, cur_out = doc["status"], doc.get("punch_in"), doc.get("punch_out")

        new_status = body.status or cur_status
        if new_status in PRESENCE:
            new_in = truncate_seconds(ms_to_dt(body.punch_in)) if body.punch_in is not None else cur_in
            new_out = truncate_seconds(ms_to_dt(body.punch_out)) if body.punch_out is not None else cur_out
            if new_in is None:
                raise HTTPException(422, "a presence status requires a punch_in")
            if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != d:
                raise HTTPException(422, "punch_in must stay on the record's attendance date")
            if new_out is not None:
                check_punch_span(new_in, new_out)
        else:  # ABSENT / LEAVE clear both punch times
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(422, "punch times cannot be supplied with status ABSENT or LEAVE")
            new_in = new_out = None

        if not (_changed(cur_status, new_status) or _changed(cur_in, new_in) or _changed(cur_out, new_out)):
            raise HTTPException(422, "the request changes nothing")

        new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                    **derive_fields(emp, d, new_status, new_in, new_out)}
        old_vals = {"status": cur_status, "punch_in": cur_in, "punch_out": cur_out,
                    "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes") or 0,
                    "overtime_minutes": doc.get("overtime_minutes") or 0, "half_day": bool(doc.get("half_day", False))}
        changes = {f: {"from": old_vals[f], "to": v} for f, v in new_vals.items() if _changed(old_vals[f], v)}
        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

        history = doc.get("history") or []
        guard = {"_id": doc["_id"], "status": cur_status, "punch_in": cur_in, "punch_out": cur_out,
                 **_no_history_guard(history)}
        res = logs.update_one(guard, {"$set": new_vals, "$push": {"history": entry}})
        if res.matched_count == 1:
            return record_out({**doc, **new_vals, "history": history + [entry]})
    raise HTTPException(409, "record was modified concurrently, please retry")


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
def working_days(start: date, end: date) -> int:
    n, day = 0, start
    while day <= end:
        n += day.weekday() < 5
        day += timedelta(days=1)
    return n


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_RE)):
    first, last = month_bounds(month)
    emp = get_employee(emp_code)
    coll, pipeline, hint = monthly_pipeline(emp_code, month)
    row = next(iter(logs.aggregate(pipeline, hint=hint)), None) or {}
    # R7: count from joined_on for mid-month joiners (0 if they join after the month)
    start = max(date.fromisoformat(first), date.fromisoformat(emp["joined_on"]))
    wd = working_days(start, date.fromisoformat(last))
    present = Decimal(str(row.get("present_days", 0)))
    pct = round_half_up(present * 100 / Decimal(wd), 2) if wd else None
    return {
        "emp_code": emp_code, "month": month, "working_days": wd,
        "present_days": float(present), "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_RE), department: Optional[str] = None):
    coll, pipeline, hint = department_summary_pipeline(month, department)
    items = []
    for r in employees.aggregate(pipeline, hint=hint):
        cnt = int(r["wh_count"])
        avg = round_half_up(Decimal(int(r["wh_cents"])) / Decimal(cnt) / 100, 2) if cnt else None
        items.append({
            "department": r["_id"], "headcount": int(r["headcount"]), "present_days": float(r["present_days"]),
            "avg_work_hours": avg, "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]), "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(pattern=MONTH_RE), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    coll, pipeline, hint = late_leaderboard_pipeline(month, limit, department)
    return {"month": month, "items": list(logs.aggregate(pipeline, hint=hint))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query()):
    start, end = trend_range(from_, to)
    coll, pipeline, hint = department_trend_pipeline(department, start, end)
    return {"department": department, "items": list(logs.aggregate(pipeline, hint=hint))}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _require(**params) -> None:
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing required query parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Each branch builds the command with the very same builder the real endpoint uses.
    if endpoint == "attendance_list":
        q = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
        coll = "attendance_logs"
        inner = {"find": coll, "filter": q["filter"], "sort": dict(q["sort"]), "skip": q["skip"], "limit": q["limit"], "hint": q["hint"]}
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            coll, pipeline, hint = monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            _require(month=month)
            coll, pipeline, hint = department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            _require(month=month)
            coll, pipeline, hint = late_leaderboard_pipeline(month, limit, department)
        else:
            _require(department=department, **{"from": from_, "to": to})
            start, end = trend_range(from_, to)
            coll, pipeline, hint = department_trend_pipeline(department, start, end)
        inner = {"aggregate": coll, "pipeline": pipeline, "cursor": {}, "hint": hint}
    result = db.command({"explain": inner, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(result))}
