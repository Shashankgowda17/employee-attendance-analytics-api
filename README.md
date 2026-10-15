# Employee Attendance & Analytics API

FastAPI + PyMongo service for employee records, attendance punches, manual regularization, monthly analytics, department trends, and MongoDB query explain plans.

## Run locally

1. Install Python 3.11+ and MongoDB 6.0+.
2. From the repository root, install dependencies with `python -m pip install -r requirements.txt`.
3. Set `MONGO_URI` and `MONGO_DB` as environment variables if you do not want the defaults (`mongodb://localhost:27017` and `attendance_db`). A local `.env` is supported; real environment variables take precedence.
4. Start the service with `python -m uvicorn app.main:app --port 8000`.
5. Check `http://127.0.0.1:8000/health` and open `http://127.0.0.1:8000/docs`.

Indexes are created idempotently at startup. Do not commit `.env`, secrets, virtual environments, or database dumps. See `openapi.yaml`, `DATA_MODEL.md`, `REVIEW.md`, and `DECISIONS.md` for the contract and implementation notes.
