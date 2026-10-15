# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All code is in `app/main.py`.

```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or use a local .env
uvicorn app.main:app --port 8000
```

Indexes are created at startup. Every endpoint in `openapi.yaml` is implemented, including `/admin/explain/{endpoint}`.

Notes:
- Queries are hinted at specific indexes so the explain plans use `IXSCAN` and never `COLLSCAN`.
- I could not run MongoDB where I wrote this, so the aggregation pipelines were reviewed by hand and not executed against a database. (**Edit this line to match what you actually tested.**)
