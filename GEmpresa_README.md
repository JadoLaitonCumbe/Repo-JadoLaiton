GEmpresa_AlphaTech_JLC — Hybrid Business Management MVP
GEmpresa is an early-stage business-management software project built with Python. It combines a web application for online access with a lightweight desktop prototype that can record basic financial movements locally and queue them for synchronization when an internet connection is available.
> **Project status:** MVP / learning project. It is not production-ready accounting software and has not been certified for legal, tax, or accounting compliance.
Project goals
Explore a web-based workflow for registering a company and signing in.
Provide a basic dashboard for a company's financial movements.
Record income and expense entries with amounts stored as integer cents.
Prototype offline data entry in a desktop client using SQLite.
Send queued desktop events to the server through an authenticated synchronization endpoint.
Practice security, validation, tenant separation, testing, and operational tooling.
Current features
Company registration and web login.
Session-based access to the dashboard.
Basic income/expense entry and a financial summary.
A desktop prototype built with Tkinter and a local SQLite database.
Device activation using a single-use, expiring activation code and a device token.
Authenticated event submission through the `/api/sync/push` endpoint, with created, duplicate, conflict, and rejected event outcomes.
Basic company scoping for stored transactions.
CSRF protection for web forms, basic security headers, and process-local request throttling.
A health endpoint at `/health`.
Smoke tests, a SQLite backup script, and a small health-endpoint concurrency test.
Features are limited to what is currently implemented in this MVP. The desktop client currently demonstrates sending queued events; a complete two-way synchronization workflow and a full conflict-resolution interface are not implemented.
Technology stack
Python 3.11+
FastAPI — web application and API
SQLite — development/server database and local desktop storage
Jinja2 — HTML templates
Tkinter — desktop user interface (usually included with Python on Windows)
pytest and HTTPX — automated tests
Repository structure
```text
alphatech_business_hybrid/
├── app/
│   ├── main.py              # FastAPI routes and application logic
│   ├── db.py                # SQLite connection and schema initialization
│   ├── security.py          # Password, secret, activation and timestamp helpers
│   ├── templates/           # HTML pages
│   └── static/              # CSS and static assets
├── scripts/
│   ├── backup_sqlite.py     # Consistent SQLite backup utility
│   └── load_test.py         # Basic concurrent /health check
├── tests/
│   └── test_smoke.py        # Smoke and security-related tests
├── desktop_client.py        # Tkinter desktop/offline prototype
├── requirements.txt
├── .env.example
└── README.md
```
Requirements
Python 3.11 or newer
`pip`
Tkinter, if you want to run the desktop client (commonly bundled with Python for Windows; on some Linux distributions it must be installed separately)
Run the web application locally
1. Open the project folder
In a terminal, change to the `alphatech_business_hybrid` directory — the folder containing `requirements.txt`.
2. Create and activate a virtual environment
Windows PowerShell
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```
Linux/macOS
```bash
python3 -m venv .venv
source .venv/bin/activate
```
3. Install dependencies
```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```
4. Configure the environment
Copy `.env.example` to `.env`.
Windows PowerShell
```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```
Linux/macOS
```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```
Put the generated random value in `.env` as `APP_SECRET_KEY`. Use at least 32 characters and do not keep the example placeholder. Keep `.env` private and never commit it to GitHub.
The example also contains `DATABASE_PATH` and `APP_MODE`. For local development, `APP_MODE=development` is appropriate. Production deployment requires additional configuration and security work; do not expose this MVP directly to the public internet as-is.
5. Start the server
```bash
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000 in your browser. Register a test company and sign in. Interactive API documentation is enabled in development only, at http://127.0.0.1:8000/docs.
Run the desktop prototype
Keep the web server running, then open a second terminal in the project folder and run:
```bash
python desktop_client.py
```
For local testing, use `http://127.0.0.1:8000` as the server URL. Register a company in the web application, obtain the activation code from the application flow, and enter it in the desktop client. The desktop prototype stores movements in a local SQLite database and can submit queued entries when the server is reachable.
For a publicly hosted server, use an HTTPS URL. The desktop client intentionally only permits plain HTTP for localhost testing.
Run tests
From the project folder, with the virtual environment activated:
```bash
pytest -q
```
Optional operational scripts
Create a SQLite backup
```bash
python scripts/backup_sqlite.py --source ./business_hybrid.db --dest ./backups
```
The backup is not encrypted. Store it in a protected location and do not publish it or commit it to a repository.
Run the basic health-endpoint concurrency check
Start the server first, then run:
```bash
python scripts/load_test.py --url http://127.0.0.1:8000/health --requests 500 --concurrency 25
```
This only exercises `/health`; it is not a full load test and does not prove the system can support production traffic.
Security notes
Configure a strong, random `APP_SECRET_KEY` before starting the app.
Never commit `.env`, database files, activation codes, device tokens, or real company/customer data.
Use HTTPS/TLS for remote deployments.
The current rate limiter is process-local and is not shared between multiple server instances.
The desktop client's local settings and database require protection on the computer where they are stored.
Known limitations and work still required
This repository is a prototype, not a production-ready or certified business/accounting platform. Before real-world deployment, it needs a security and architecture review, including at least:
PostgreSQL or another production-grade database strategy, migrations, connection pooling, and concurrency testing.
Multi-factor authentication, stronger account recovery, granular roles/permissions, and a shared rate limiter.
A reviewed secret/token storage strategy for the desktop client.
Complete two-way synchronization, conflict-resolution workflows, retry/recovery handling, and end-to-end offline tests.
Encrypted backups, off-server copies, and verified restore drills.
Comprehensive audit logging and authorization/isolation tests.
Production deployment configuration, monitoring, error handling, and load tests for actual business workflows.
Review of applicable Mozambican accounting, tax, privacy, and business requirements before using it for real financial records.
Learning and portfolio use
This project can be presented as a personal software-development project demonstrating work on a Python/FastAPI application, SQLite persistence, a Tkinter desktop prototype, basic API authentication, offline event queuing, and automated smoke tests. Describe only the parts you personally implemented and can explain or demonstrate. Clearly label unfinished items as planned or in progress.
License
No license is specified in this repository yet. Add a license file before allowing others to reuse or distribute the project.
