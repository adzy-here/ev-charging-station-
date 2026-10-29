\# Zero Latency ⚡



\*\*Zero Latency\*\* is a web-based EV charging marketplace prototype designed to help drivers discover nearby charging stations, compare connector compatibility and pricing, plan a trip, reserve a time slot, and follow a charging session. It also provides tools for charging hosts to manage stations, availability, bookings, pricing, reviews, and session issues.



> \*\*Project status:\*\* Hackathon prototype. Charging telemetry, OTP delivery, and route-planner reservations include demo/simulated behavior. Do not treat the demo OTP as an SMS-based verification service or the planner reservation as a real booking/payment.



\## Highlights



\### For EV drivers (consumers)

\- Search for active charging stations by location, radius, and connector type.

\- Sort recommendations by relevance, distance, price, or rating.

\- Review station details, estimated prices, availability, reliability information, reviews, and issue reports.

\- Plan a route and compare candidate charging stops using a 15% range safety buffer.

\- Create and view bookings, verify a demo OTP with the host, start a verified session, and view estimated session progress.

\- View completed-session billing details and estimated environmental impact.



\### For charging hosts

\- Create stations and manage station details, status, prices, and future availability slots.

\- View host analytics such as earnings, completed sessions, energy delivered, platform fees, stations, bookings, reviews, and strikes.

\- Verify consumer booking OTPs and start eligible sessions.

\- Record consumer strikes and report charging interruptions.

\- Configure time-based pricing rules.



\### Shared experience

\- Role-aware host and consumer flows with JWT bearer authentication.

\- Responsive interface with light/dark/system theme selection.

\- Google Maps-based station discovery and trip planning, with backend address lookup via OpenStreetMap Nominatim.

\- Terms and conditions page and a separate payment/receipt page.



\## Tech stack



\- \*\*Frontend:\*\* HTML, CSS, and vanilla JavaScript

\- \*\*Backend:\*\* Python, FastAPI, Pydantic

\- \*\*Database:\*\* MySQL 8.0.16 or newer

\- \*\*Authentication:\*\* JWT sessions and bcrypt password hashing

\- \*\*Maps:\*\* Google Maps JavaScript API (Maps, Places API (New), and Routes API); OpenStreetMap Nominatim is used for backend address geocoding

\- \*\*Testing:\*\* Python `unittest` test suite



\## Repository layout



```text

voltway/

├── main.py                              # FastAPI application and API routes

├── schema.sql                           # Fresh-install MySQL schema

├── 002\_schema\_hardening.sql             # Schema hardening for existing installations

├── migrations/

│   ├── 003\_fix\_trigger\_locking.sql

│   ├── 004\_consumer\_strikes.sql

│   ├── 005\_station\_address.sql

│   ├── 006\_consumer\_otp.sql

│   ├── 007\_host\_session\_start.sql

│   ├── 008\_profile\_features.sql

│   └── 009\_session\_interruptions.sql

├── zero\_latency\_interactive\_preview\_human\_copy.html  # HTML served by GET /

├── index (5).html                       # Additional frontend build/iteration in archive

├── host-profile.html                    # Host profile page

├── consumer-profile.html                # Consumer profile page

├── profile-pages.js                     # Profile page behavior

├── profile-pages.css                    # Profile page styling

├── zero\_latency\_payment.html            # Payment/receipt UI

├── terms.html                            # Terms and conditions

├── privacy.html                          # Privacy page file (currently empty in archive)

├── DEMO\_OTP\_SETUP.md                    # Demo OTP and migration notes

├── dist/                                 # Frontend/map assets

└── tests/                                # Automated tests

```



The FastAPI root route serves `zero\_latency\_interactive\_preview\_human\_copy.html`. That is the page loaded at `http://127.0.0.1:8000/` when the API is running.



\## Prerequisites



\- Python 3.10+ (use a Python version supported by the dependencies in your environment)

\- MySQL 8.0.16+

\- A MySQL user with permission to create/use the `ev\_charging` database and apply its schema

\- For map and route-planning features, a Google Maps API key with the required APIs enabled and appropriate website restrictions



\## Setup



\### 1. Get the code



Clone the GitHub repository or extract the project archive. Change into the directory containing `main.py`:



```bash

cd voltway

```



\### 2. Create and activate a virtual environment



\*\*Windows PowerShell:\*\*



```powershell

py -m venv .venv

.\\.venv\\Scripts\\Activate.ps1

```



\*\*macOS/Linux:\*\*



```bash

python3 -m venv .venv

source .venv/bin/activate

```



\### 3. Install backend dependencies



The archive does not include a `requirements.txt`, so install the packages used by the backend explicitly:



```bash

python -m pip install --upgrade pip

python -m pip install fastapi uvicorn mysql-connector-python bcrypt PyJWT email-validator

```



If you add or change dependencies, capture the verified environment in a `requirements.txt` before sharing the repository.



\### 4. Configure MySQL



Start MySQL, then load the schema for a \*\*fresh database\*\* from the project directory:



```bash

mysql -u root -p < schema.sql

```



The schema creates and selects a database named `ev\_charging`. You may use a dedicated least-privilege database user rather than `root` for application access.



For an \*\*existing installation\*\*, take a database backup first and apply the hardening/migrations in this order:



```text

002\_schema\_hardening.sql

migrations/003\_fix\_trigger\_locking.sql

migrations/004\_consumer\_strikes.sql

migrations/005\_station\_address.sql

migrations/006\_consumer\_otp.sql

migrations/007\_host\_session\_start.sql

migrations/008\_profile\_features.sql

migrations/009\_session\_interruptions.sql

```



Run each script against the `ev\_charging` database. `CREATE TABLE IF NOT EXISTS` does not alter existing table definitions; read each migration and verify the database state before applying it. The hardening script is at the project root in this archive, even though the comment at the top of `schema.sql` refers to it under `migrations/`.



\### 5. Configure environment variables



Set configuration in your shell or in a local, untracked environment file. Do not commit credentials or signing secrets.



\*\*Windows PowerShell example:\*\*



```powershell

$env:DB\_HOST = '127.0.0.1'

$env:DB\_PORT = '3306'

$env:DB\_USER = 'your\_app\_db\_user'

$env:DB\_PASSWORD = 'your\_local\_db\_password'

$env:DB\_NAME = 'ev\_charging'

$env:JWT\_SECRET = (python -c "import secrets; print(secrets.token\_hex(32))")

$env:OTP\_HASH\_SECRET = (python -c "import secrets; print(secrets.token\_hex(32))")

$env:TOKEN\_HOURS = '24'

$env:CORS\_ORIGINS = 'http://127.0.0.1:8000,http://localhost:8000'

```



Equivalent shell variables on macOS/Linux:



```bash

export DB\_HOST=127.0.0.1

export DB\_PORT=3306

export DB\_USER=your\_app\_db\_user

export DB\_PASSWORD=your\_local\_db\_password

export DB\_NAME=ev\_charging

export JWT\_SECRET="$(python -c 'import secrets; print(secrets.token\_hex(32))')"

export OTP\_HASH\_SECRET="$(python -c 'import secrets; print(secrets.token\_hex(32))')"

export TOKEN\_HOURS=24

export CORS\_ORIGINS='http://127.0.0.1:8000,http://localhost:8000'

```



The code currently has fallback defaults for database settings and a JWT secret. \*\*Override the JWT secret before running the application\*\*, and use strong, unique secrets outside local development. `OTP\_HASH\_SECRET` is optional in the code but should be set to a separate random secret for deployments.



\### 6. Configure Google Maps



The frontend uses Google Maps features for the map and route planner. Before publishing or deploying:



1\. Create your own Google Maps API key.

2\. Enable the Maps JavaScript API, Places API (New), and Routes API, along with billing as required by Google.

3\. Restrict the key to the intended website origins and only the APIs it needs.

4\. Replace the key value in the frontend configuration. Do not commit a real key or secrets; if a key already included in a shared file is yours, rotate it and restrict the replacement.



The map planner uses \*\*illustrative in-page station data\*\* for recommendations. Its reservation confirmation is a demo state only and is not sent to the booking API.



\### 7. Start the backend and frontend



From the directory containing `main.py`:



```bash

python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000

```



Open:



\- \*\*Website:\*\* http://127.0.0.1:8000/

\- \*\*Swagger API docs:\*\* http://127.0.0.1:8000/docs

\- \*\*OpenAPI schema:\*\* http://127.0.0.1:8000/openapi.json

\- \*\*Health check:\*\* http://127.0.0.1:8000/health



The health endpoint checks the MySQL connection. If the API cannot reach MySQL, verify the database is running, the `ev\_charging` schema exists, and the environment variables match your setup.



\## API overview



The backend is implemented in `main.py`; the interactive API documentation at `/docs` is the source of truth for request and response schemas.



| Area | Representative routes | Purpose |

|---|---|---|

| Health and geocoding | `GET /health`, `GET /geocode` | Check backend/database availability and look up addresses |

| Authentication | `POST /auth/register`, `POST /auth/login`, `GET /auth/me`, `POST /auth/logout` | Register, sign in, inspect the current session, and revoke the current session |

| Station discovery | `GET /stations/recommendations`, `GET /stations/{station\_id}`, `GET /stations/{station\_id}/quote` | Search and inspect stations and estimated pricing |

| Host station management | `POST /stations`, `PATCH /stations/{station\_id}`, `PATCH /stations/{station\_id}/status`, `DELETE /stations/{station\_id}` | Manage host-owned stations |

| Availability and pricing | `GET/POST /stations/{station\_id}/slots`, `PUT/DELETE /slots/{slot\_id}`, `/stations/{station\_id}/pricing-rules` | Manage time slots and pricing rules |

| Bookings and sessions | `POST /bookings`, `GET /bookings/mine`, `/bookings/{booking\_id}/otp/\*`, `/bookings/{booking\_id}/start`, `/bookings/{booking\_id}/stop`, `/sessions/{booking\_id}/live` | Create and manage bookings and charging-session lifecycle |

| Feedback and issues | `/stations/{station\_id}/reviews`, `/stations/{station\_id}/reports`, `/stations/{station\_id}/reliability` | Review stations, report problems, and inspect reliability signals |

| Host and consumer data | `GET /analytics/host`, `GET /consumer/strikes/mine`, `GET /impact/mine`, `GET /notifications/mine` | Host analytics, consumer strikes, estimated impact, and notifications |

| Recommendations | `POST /assistant/charge` | Return rule-based station suggestions from the backend |



Most private endpoints require `Authorization: Bearer <token>`. Host and consumer routes enforce role checks, and station/bookings operations validate ownership in the backend. Consult `main.py` for exact access requirements and route details.



\## Demo OTP and session lifecycle



OTP behavior is documented in \[`DEMO\_OTP\_SETUP.md`](DEMO\_OTP\_SETUP.md). In this repository's demo implementation:



\- The app does \*\*not\*\* send SMS and does not require an SMS-provider account.

\- A booking-specific six-digit code is generated for the demo and only its keyed hash is persisted.

\- The consumer shows the demo code to the assigned host, who verifies it through the API.

\- The session is started only after verification; the consumer or assigned host can then start the eligible booking.

\- The code has an expiry, a limited number of verification attempts, and resend limits/cooldown.



This is suitable for a controlled demonstration, not a substitute for a production SMS or identity-verification flow. Do not expose development OTP values or enable demo-mode behavior in a public production deployment.



\## Run the tests



From the `voltway` directory, run the standard-library test suite:



```bash

python -m unittest discover -s tests -v

```



The archive includes tests covering consumer OTP/session behavior, registration terms, and charging-session interruptions. These tests mock parts of the database layer; a passing unit-test suite does not replace integration testing against a clean MySQL database.



\## Security and deployment checklist



Before making the project public or deploying it beyond a local demo:



\- Replace all default database credentials and fallback signing secrets; use a least-privilege database user.

\- Remove credentials, local `.env` files, virtual environments, `\_\_pycache\_\_`, and `.pyc` files from the Git repository.

\- Rotate and restrict any Google Maps key exposed in a shared source file.

\- Set `CORS\_ORIGINS` to the exact origins used by the deployed frontend instead of leaving a wildcard configuration.

\- Use HTTPS, appropriate security headers, production logging, backups, and a managed secrets mechanism.

\- Review account lifecycle, privacy disclosures, retention, and payment handling before accepting real users or money. The `privacy.html` file in the supplied archive is empty and should be completed or removed until a real privacy notice is available.

\- Replace simulated telemetry, demo OTP delivery, and planner-only reservations with verified production integrations before representing them as live services.

\- Re-test authentication, role restrictions, ownership rules, booking concurrency, migrations, and failure cases against the deployment database.



\## Notes on the repository archive



The supplied archive includes a local `venv/`, Python cache files, and compiled test files. These are machine-specific artifacts and should generally not be committed to GitHub. Keep the source code, SQL schema/migrations, tests, documentation, and required frontend assets instead. Consider adding a `.gitignore` and `requirements.txt` to make the repository easier to reproduce.



\## License



No license file was present in the supplied archive. Add a `LICENSE` file if you intend to specify reuse, distribution, or contribution terms.



