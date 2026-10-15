"""Optional end-to-end check. Start the API first, then:  python smoke_test.py [base_url]
Creates its own uniquely named department/employees, so it can run against a non-empty database.
Expected values were worked out by hand from the business rules (see comments)."""
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
IST = timezone(timedelta(hours=5, minutes=30))
c = httpx.Client(base_url=BASE, timeout=30)
fails = 0


def check(name, cond, extra=""):
    global fails
    fails += not cond
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"   -> {extra}"))


def ms(y, m, d, h, mi, s=0):
    return int(datetime(y, m, d, h, mi, s, tzinfo=IST).timestamp() * 1000)


run = random.randint(100000, 999999) // 10 * 10
DEPT = f"Smoke{run}"
code = lambda i: f"EMP{run + i}"  # noqa: E731
A, B, C, E, F = (code(i) for i in range(5))
for emp, joined in ((A, "2026-01-01"), (B, "2026-04-15"), (C, "2026-01-01"), (E, "2026-01-01"), (F, "2026-01-01")):
    r = c.post("/employees", json={"emp_code": emp, "name": emp, "email": "x@y.co", "department": DEPT, "joined_on": joined})
    check(f"create {emp}", r.status_code == 201, r.text)
check("duplicate employee -> 409", c.post("/employees", json={"emp_code": A, "name": "x", "email": "x@y.co", "department": DEPT, "joined_on": "2026-01-01"}).status_code == 409)


def punch(emp, d, hin, min_, hout=None, mout=None, sin=0):
    r = c.post("/attendance/punch-in", json={"emp_code": emp, "punched_at": ms(2026, 4, d, hin, min_, sin)})
    assert r.status_code == 201, r.text
    if hout is not None:
        r = c.post("/attendance/punch-out", json={"emp_code": emp, "punched_at": ms(2026, 4, d, hout, mout)})
        assert r.status_code == 200, r.text
        return r.json()
    return None


for emp in (A, E):
    r1 = punch(emp, 6, 9, 41, 18, 50)       # late 11, 9.15 h
    r2 = punch(emp, 7, 10, 15, 19, 10, sin=59)  # late 45, overtime 40, 8.90 h
    r3 = punch(emp, 8, 9, 0, 12, 0)         # 3.00 h -> half day
    punch(emp, 11, 9, 50)                   # Saturday, late 20, no punch-out
check("late 11 / hours 9.15 / no overtime", (r1["late_minutes"], r1["work_hours"], r1["overtime_minutes"]) == (11, 9.15, 0), r1)
check("late 45 / overtime 40 / 8.9 h", (r2["late_minutes"], r2["overtime_minutes"], r2["work_hours"]) == (45, 40, 8.9), r2)
check("half day", r3["half_day"] is True and r3["work_hours"] == 3.0, r3)
punch(F, 6, 10, 0)                          # late exactly 30
punch(B, 16, 9, 30, 18, 30)

# overnight shift
N = f"EMP{run + 5}"
c.post("/employees", json={"emp_code": N, "name": N, "email": "x@y.co", "department": DEPT + "N", "joined_on": "2026-01-01", "shift_start": "22:00", "shift_end": "06:00"})
r = c.post("/attendance/punch-in", json={"emp_code": N, "punched_at": ms(2026, 4, 7, 0, 30)})
check("overnight punch-in at 00:30 belongs to previous day", r.status_code == 201 and r.json()["date"] == "2026-04-06", r.text)
r = c.post("/attendance/punch-out", json={"emp_code": N, "punched_at": ms(2026, 4, 7, 6, 40)})
check("overnight punch-out finds the record, overtime 40", r.status_code == 200 and r.json()["overtime_minutes"] == 40, r.text)

# concurrency
G = f"EMP{run + 6}"
c.post("/employees", json={"emp_code": G, "name": G, "email": "x@y.co", "department": DEPT + "G", "joined_on": "2026-01-01"})
with ThreadPoolExecutor(20) as ex:
    codes = list(ex.map(lambda _: httpx.post(BASE + "/attendance/punch-in", json={"emp_code": G, "punched_at": ms(2026, 4, 6, 9, 0)}).status_code, range(20)))
check("20 parallel punch-ins -> one 201, rest 409", codes.count(201) == 1 and codes.count(409) == 19, codes)
with ThreadPoolExecutor(20) as ex:
    codes = list(ex.map(lambda _: httpx.post(BASE + "/attendance/punch-out", json={"emp_code": G, "punched_at": ms(2026, 4, 6, 18, 0)}).status_code, range(20)))
check("20 parallel punch-outs -> one 200", codes.count(200) == 1, codes)

# regularization
r = c.patch(f"/attendance/{A}/2026-04-07", json={"punch_in": ms(2026, 4, 7, 9, 28), "reason": "biometric glitch", "regularized_by": "hr"})
j = r.json()
check("regularize recomputes and appends history", r.status_code == 200 and j["late_minutes"] == 0 and len(j["history"]) == 1
      and set(j["history"][0]["changes"]) == {"punch_in", "late_minutes", "work_hours"}, r.text)
check("same change again -> 422", c.patch(f"/attendance/{A}/2026-04-07", json={"punch_in": ms(2026, 4, 7, 9, 28), "reason": "again again", "regularized_by": "hr"}).status_code == 422)
check("patch unknown date -> 404", c.patch(f"/attendance/{A}/2026-04-30", json={"status": "LEAVE", "reason": "no record", "regularized_by": "hr"}).status_code == 404)
# put it back so the analytics expectations below hold
c.patch(f"/attendance/{A}/2026-04-07", json={"punch_in": ms(2026, 4, 7, 10, 15, 59), "reason": "restore value", "regularized_by": "hr"})

# analytics.  A: working_days 22, present 1+1+0.5 = 2.5 (Saturday excluded), late 11+45+20 = 76 in 3 records
r = c.get(f"/analytics/employees/{A}/monthly", params={"month": "2026-04"}).json()
check("monthly A", (r["working_days"], r["present_days"], r["late_count"], r["total_late_minutes"], r["total_overtime_minutes"], r["attendance_pct"]) == (22, 2.5, 3, 76, 40, 11.36), r)
r = c.get(f"/analytics/employees/{B}/monthly", params={"month": "2026-04"}).json()
check("monthly B (joined 15 April: 12 working days)", (r["working_days"], r["present_days"], r["attendance_pct"]) == (12, 1.0, 8.33), r)
check("monthly unknown employee -> 404", c.get("/analytics/employees/EMP999999/monthly", params={"month": "2026-04"}).status_code == 404)

r = c.get("/analytics/departments/summary", params={"month": "2026-04", "department": DEPT}).json()["items"]
check("summary: headcount 5 incl. employee without logs", len(r) == 1 and r[0]["headcount"] == 5, r)
# present 2.5+2.5+1 (F) +1 (B) = 7.0; late 3+3+1 = 7 records, 76+76+30 = 182 min; hours (9.15+8.90+3)*2+9.0 = 51.1 over 7 records = 7.3
check("summary values", r and (r[0]["present_days"], r[0]["late_count"], r[0]["total_late_minutes"], r[0]["avg_work_hours"]) == (7.0, 7, 182, 7.3), r)

r = c.get("/analytics/leaderboard/late", params={"month": "2026-04", "department": DEPT, "limit": 1}).json()["items"]
check("leaderboard tie at cutoff returns both, ordered by emp_code", [(x["rank"], x["emp_code"]) for x in r] == [(1, A), (1, E)], r)
r = c.get("/analytics/leaderboard/late", params={"month": "2026-04", "department": DEPT, "limit": 3}).json()["items"]
check("leaderboard ranks 1,1,3", [x["rank"] for x in r] == [1, 1, 3], r)

r = c.get(f"/analytics/departments/{DEPT}/trend", params={"from": "2026-04-01", "to": "2026-04-12"}).json()["items"]
check("trend has 12 rows", len(r) == 12, len(r))
by = {x["date"]: x for x in r}
check("trend weekend has null rate, headcount 4", by["2026-04-04"]["attendance_rate"] is None and by["2026-04-04"]["headcount"] == 4, by.get("2026-04-04"))
check("trend rates 0.75 / 0.5 / 0.25", (by["2026-04-06"]["attendance_rate"], by["2026-04-07"]["attendance_rate"], by["2026-04-08"]["attendance_rate"]) == (0.75, 0.5, 0.25), r)
check("trend empty working day is 0.0, moving avg first row", by["2026-04-01"]["attendance_rate"] == 0.0 and by["2026-04-01"]["moving_avg_7d"] == 0.0, by["2026-04-01"])
check("trend moving average 0.1875 on 6 April", by["2026-04-06"]["moving_avg_7d"] == 0.1875, by["2026-04-06"])
check("trend unknown department -> 404", c.get("/analytics/departments/NoSuchDept/trend", params={"from": "2026-04-01", "to": "2026-04-02"}).status_code == 404)

# list + explain
r = c.get("/attendance", params={"emp_code": A, "page_size": 2}).json()
check("attendance list sorted by date desc, total counts filter", r["total"] == 4 and [x["date"] for x in r["items"]] == ["2026-04-11", "2026-04-08"], r)
for ep, params in {
    "attendance_list": {"emp_code": A}, "employee_monthly": {"emp_code": A, "month": "2026-04"},
    "department_summary": {"month": "2026-04"}, "late_leaderboard": {"month": "2026-04"},
    "department_trend": {"department": DEPT, "from": "2026-04-01", "to": "2026-04-30"},
}.items():
    r = c.get(f"/admin/explain/{ep}", params=params)
    txt = r.text
    check(f"explain {ep}: IXSCAN and no COLLSCAN", r.status_code == 200 and "IXSCAN" in txt and "COLLSCAN" not in txt, r.text[:200])

print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
