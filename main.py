"""
EV Charging Marketplace - FastAPI backend (MySQL 8.x)

Run:
    pip install -r requirements.txt
    cp .env.example .env   # then edit
    mysql < schema.sql     # create the database/tables first
    uvicorn main:app --reload

Interactive docs: http://localhost:8000/docs
"""
import math
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

import bcrypt
import jwt
import pymysql
import pymysql.cursors
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field, model_validator

load_dotenv()

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
DB_CONFIG = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "ev_charging"),
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
    autocommit=False,
)
JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "24"))
PLATFORM_FEE_PERCENT = Decimal(os.getenv("PLATFORM_FEE_PERCENT", "10"))
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")

app = FastAPI(title="EV Charging Marketplace API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
bearer = HTTPBearer()


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------
def get_db():
    """One connection per request; commit on success, rollback on error."""
    conn = pymysql.connect(**DB_CONFIG)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def cursor(conn):
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


def new_id() -> str:
    return str(uuid.uuid4())


def money(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_token(user_id: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    conn=Depends(get_db),
) -> dict:
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    with cursor(conn) as cur:
        cur.execute(
            "SELECT id, name, email, role, created_at FROM users WHERE id=%s",
            (payload["sub"],),
        )
        user = cur.fetchone()
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")
    return user


def require_role(role: str):
    def checker(user: dict = Depends(current_user)) -> dict:
        if user["role"] != role:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Only {role}s can do this")
        return user

    return checker


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------
class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    role: Literal["consumer", "host"] = "consumer"


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class StationIn(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    connector_type: str = Field(min_length=1, max_length=50)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    pricing_type: Literal["per_kwh", "per_hour"]
    price_rate: Decimal = Field(gt=0, max_digits=10, decimal_places=2)


class StationStatusIn(BaseModel):
    status: Literal["active", "inactive", "maintenance"]


class SlotIn(BaseModel):
    start_time: datetime
    end_time: datetime

    @model_validator(mode="after")
    def check_order(self):
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be before end_time")
        return self


class BookingIn(BaseModel):
    slot_id: str


class StopChargingIn(BaseModel):
    energy_kwh: Decimal = Field(ge=0, max_digits=6, decimal_places=2)


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------
@app.post("/auth/register", status_code=201, tags=["auth"])
def register(body: RegisterIn, conn=Depends(get_db)):
    with cursor(conn) as cur:
        cur.execute("SELECT 1 FROM users WHERE email=%s", (body.email,))
        if cur.fetchone():
            raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
        uid = new_id()
        cur.execute(
            "INSERT INTO users (id, name, email, password_hash, role) VALUES (%s,%s,%s,%s,%s)",
            (uid, body.name, body.email, hash_password(body.password), body.role),
        )
    return {"id": uid, "token": create_token(uid, body.role), "role": body.role}


@app.post("/auth/login", tags=["auth"])
def login(body: LoginIn, conn=Depends(get_db)):
    with cursor(conn) as cur:
        cur.execute(
            "SELECT id, role, password_hash FROM users WHERE email=%s", (body.email,)
        )
        user = cur.fetchone()
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    return {"token": create_token(user["id"], user["role"]), "role": user["role"]}


@app.get("/auth/me", tags=["auth"])
def me(user: dict = Depends(current_user)):
    return user


# --------------------------------------------------------------------------
# Stations
# --------------------------------------------------------------------------
STATION_COLUMNS = """
    s.id, s.host_id, s.title, s.connector_type, s.pricing_type, s.price_rate,
    s.status, s.created_at,
    ST_Latitude(s.coordinates)  AS latitude,
    ST_Longitude(s.coordinates) AS longitude
"""


def point_wkt(lat: float, lng: float) -> str:
    # MySQL 8 with SRID 4326 uses (latitude longitude) axis order.
    return f"POINT({lat} {lng})"


@app.post("/stations", status_code=201, tags=["stations"])
def create_station(
    body: StationIn, host: dict = Depends(require_role("host")), conn=Depends(get_db)
):
    sid = new_id()
    with cursor(conn) as cur:
        cur.execute(
            """INSERT INTO charging_stations
               (id, host_id, title, connector_type, coordinates, pricing_type, price_rate)
               VALUES (%s,%s,%s,%s,ST_GeomFromText(%s, 4326),%s,%s)""",
            (
                sid, host["id"], body.title, body.connector_type,
                point_wkt(body.latitude, body.longitude),
                body.pricing_type, body.price_rate,
            ),
        )
    return {"id": sid}


@app.get("/stations/nearby", tags=["stations"])
def nearby_stations(
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
    radius_km: float = Query(10, gt=0, le=200),
    connector_type: Optional[str] = None,
    only_available: bool = False,
    limit: int = Query(50, ge=1, le=200),
    conn=Depends(get_db),
):
    """Active stations within radius_km, nearest first (distance in meters)."""
    # Bounding box lets the SPATIAL INDEX prune rows before exact distance.
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(abs(math.cos(math.radians(lat))), 0.01))
    s, n = max(lat - dlat, -90), min(lat + dlat, 90)
    w, e = max(lng - dlng, -180), min(lng + dlng, 180)
    box = f"POLYGON(({s} {w},{s} {e},{n} {e},{n} {w},{s} {w}))"

    sql = f"""
        SELECT {STATION_COLUMNS},
               ST_Distance(s.coordinates, ST_GeomFromText(%s, 4326)) AS distance_m
        FROM charging_stations s
        WHERE s.status = 'active'
          AND MBRContains(ST_GeomFromText(%s, 4326), s.coordinates)
          AND ST_Distance(s.coordinates, ST_GeomFromText(%s, 4326)) <= %s
    """
    here = point_wkt(lat, lng)
    params = [here, box, here, radius_km * 1000]
    if connector_type:
        sql += " AND s.connector_type = %s"
        params.append(connector_type)
    if only_available:
        sql += """ AND EXISTS (SELECT 1 FROM time_slots t
                               WHERE t.station_id = s.id AND t.is_booked = FALSE
                                 AND t.start_time > NOW())"""
    sql += " ORDER BY distance_m LIMIT %s"
    params.append(limit)

    with cursor(conn) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


@app.get("/stations/mine", tags=["stations"])
def my_stations(host: dict = Depends(require_role("host")), conn=Depends(get_db)):
    with cursor(conn) as cur:
        cur.execute(
            f"SELECT {STATION_COLUMNS} FROM charging_stations s WHERE s.host_id=%s "
            "ORDER BY s.created_at DESC",
            (host["id"],),
        )
        return cur.fetchall()


@app.get("/stations/{station_id}", tags=["stations"])
def get_station(station_id: str, conn=Depends(get_db)):
    with cursor(conn) as cur:
        cur.execute(
            f"SELECT {STATION_COLUMNS} FROM charging_stations s WHERE s.id=%s",
            (station_id,),
        )
        station = cur.fetchone()
    if not station:
        raise HTTPException(404, "Station not found")
    return station


def owned_station(cur, station_id: str, host_id: str) -> dict:
    cur.execute("SELECT id, host_id FROM charging_stations WHERE id=%s", (station_id,))
    st = cur.fetchone()
    if not st:
        raise HTTPException(404, "Station not found")
    if st["host_id"] != host_id:
        raise HTTPException(403, "Not your station")
    return st


@app.patch("/stations/{station_id}/status", tags=["stations"])
def set_station_status(
    station_id: str,
    body: StationStatusIn,
    host: dict = Depends(require_role("host")),
    conn=Depends(get_db),
):
    with cursor(conn) as cur:
        owned_station(cur, station_id, host["id"])
        cur.execute(
            "UPDATE charging_stations SET status=%s WHERE id=%s", (body.status, station_id)
        )
    return {"id": station_id, "status": body.status}


@app.delete("/stations/{station_id}", status_code=204, tags=["stations"])
def delete_station(
    station_id: str, host: dict = Depends(require_role("host")), conn=Depends(get_db)
):
    with cursor(conn) as cur:
        owned_station(cur, station_id, host["id"])
        try:
            cur.execute("DELETE FROM charging_stations WHERE id=%s", (station_id,))
        except pymysql.err.IntegrityError:
            raise HTTPException(
                409, "Station has bookings and cannot be deleted; set it to inactive instead"
            )


# --------------------------------------------------------------------------
# Time slots
# --------------------------------------------------------------------------
@app.post("/stations/{station_id}/slots", status_code=201, tags=["slots"])
def create_slot(
    station_id: str,
    body: SlotIn,
    host: dict = Depends(require_role("host")),
    conn=Depends(get_db),
):
    with cursor(conn) as cur:
        owned_station(cur, station_id, host["id"])
        cur.execute(
            """SELECT 1 FROM time_slots
               WHERE station_id=%s AND start_time < %s AND end_time > %s LIMIT 1""",
            (station_id, body.end_time, body.start_time),
        )
        if cur.fetchone():
            raise HTTPException(409, "Slot overlaps an existing slot")
        slot_id = new_id()
        cur.execute(
            "INSERT INTO time_slots (id, station_id, start_time, end_time) VALUES (%s,%s,%s,%s)",
            (slot_id, station_id, body.start_time, body.end_time),
        )
    return {"id": slot_id}


@app.get("/stations/{station_id}/slots", tags=["slots"])
def list_slots(
    station_id: str,
    available_only: bool = True,
    conn=Depends(get_db),
):
    sql = "SELECT * FROM time_slots WHERE station_id=%s AND end_time > NOW()"
    if available_only:
        sql += " AND is_booked = FALSE"
    sql += " ORDER BY start_time"
    with cursor(conn) as cur:
        cur.execute(sql, (station_id,))
        return cur.fetchall()


@app.delete("/slots/{slot_id}", status_code=204, tags=["slots"])
def delete_slot(
    slot_id: str, host: dict = Depends(require_role("host")), conn=Depends(get_db)
):
    with cursor(conn) as cur:
        cur.execute(
            """SELECT t.id, t.is_booked, s.host_id FROM time_slots t
               JOIN charging_stations s ON s.id = t.station_id WHERE t.id=%s""",
            (slot_id,),
        )
        slot = cur.fetchone()
        if not slot:
            raise HTTPException(404, "Slot not found")
        if slot["host_id"] != host["id"]:
            raise HTTPException(403, "Not your slot")
        if slot["is_booked"]:
            raise HTTPException(409, "Slot is booked")
        cur.execute("DELETE FROM time_slots WHERE id=%s", (slot_id,))


# --------------------------------------------------------------------------
# Bookings
# --------------------------------------------------------------------------
@app.post("/bookings", status_code=201, tags=["bookings"])
def create_booking(
    body: BookingIn,
    consumer: dict = Depends(require_role("consumer")),
    conn=Depends(get_db),
):
    """Reserves a slot. The row lock prevents two users booking the same slot."""
    with cursor(conn) as cur:
        cur.execute(
            """SELECT t.id, t.station_id, t.is_booked, t.start_time, s.status AS station_status
               FROM time_slots t JOIN charging_stations s ON s.id = t.station_id
               WHERE t.id=%s FOR UPDATE""",
            (body.slot_id,),
        )
        slot = cur.fetchone()
        if not slot:
            raise HTTPException(404, "Slot not found")
        if slot["is_booked"]:
            raise HTTPException(409, "Slot already booked")
        if slot["station_status"] != "active":
            raise HTTPException(409, "Station is not accepting bookings")
        if slot["start_time"] <= datetime.now():
            raise HTTPException(409, "Slot has already started")

        booking_id = new_id()
        cur.execute(
            """INSERT INTO bookings (id, consumer_id, station_id, slot_id)
               VALUES (%s,%s,%s,%s)""",
            (booking_id, consumer["id"], slot["station_id"], slot["id"]),
        )
        cur.execute("UPDATE time_slots SET is_booked=TRUE WHERE id=%s", (slot["id"],))
    return {"id": booking_id, "status": "reserved"}


@app.get("/bookings/mine", tags=["bookings"])
def my_bookings(user: dict = Depends(current_user), conn=Depends(get_db)):
    """Consumers see their bookings; hosts see bookings at their stations."""
    if user["role"] == "host":
        where, arg = "s.host_id = %s", user["id"]
    else:
        where, arg = "b.consumer_id = %s", user["id"]
    with cursor(conn) as cur:
        cur.execute(
            f"""SELECT b.*, s.title AS station_title, t.start_time AS slot_start,
                       t.end_time AS slot_end
                FROM bookings b
                JOIN charging_stations s ON s.id = b.station_id
                JOIN time_slots t ON t.id = b.slot_id
                WHERE {where} ORDER BY t.start_time DESC""",
            (arg,),
        )
        return cur.fetchall()


def load_booking_for_consumer(cur, booking_id: str, consumer_id: str) -> dict:
    cur.execute("SELECT * FROM bookings WHERE id=%s FOR UPDATE", (booking_id,))
    b = cur.fetchone()
    if not b:
        raise HTTPException(404, "Booking not found")
    if b["consumer_id"] != consumer_id:
        raise HTTPException(403, "Not your booking")
    return b


@app.post("/bookings/{booking_id}/cancel", tags=["bookings"])
def cancel_booking(
    booking_id: str,
    consumer: dict = Depends(require_role("consumer")),
    conn=Depends(get_db),
):
    with cursor(conn) as cur:
        b = load_booking_for_consumer(cur, booking_id, consumer["id"])
        if b["status"] != "reserved":
            raise HTTPException(409, f"Cannot cancel a booking that is {b['status']}")
        cur.execute("UPDATE bookings SET status='cancelled' WHERE id=%s", (booking_id,))
        cur.execute("UPDATE time_slots SET is_booked=FALSE WHERE id=%s", (b["slot_id"],))
    return {"id": booking_id, "status": "cancelled"}


@app.post("/bookings/{booking_id}/start", tags=["bookings"])
def start_charging(
    booking_id: str,
    consumer: dict = Depends(require_role("consumer")),
    conn=Depends(get_db),
):
    with cursor(conn) as cur:
        b = load_booking_for_consumer(cur, booking_id, consumer["id"])
        if b["status"] != "reserved":
            raise HTTPException(409, f"Booking is {b['status']}, cannot start")
        cur.execute(
            "UPDATE bookings SET status='charging', started_at=NOW() WHERE id=%s",
            (booking_id,),
        )
    return {"id": booking_id, "status": "charging"}


@app.post("/bookings/{booking_id}/stop", tags=["bookings"])
def stop_charging(
    booking_id: str,
    body: StopChargingIn,
    consumer: dict = Depends(require_role("consumer")),
    conn=Depends(get_db),
):
    """
    Ends the session, prices it and creates a pending transaction.
    energy_kwh is client-supplied here; in production take it from the
    charger / meter (e.g. via OCPP) instead of trusting the client.
    """
    with cursor(conn) as cur:
        b = load_booking_for_consumer(cur, booking_id, consumer["id"])
        if b["status"] != "charging":
            raise HTTPException(409, f"Booking is {b['status']}, cannot stop")

        cur.execute(
            "SELECT pricing_type, price_rate FROM charging_stations WHERE id=%s",
            (b["station_id"],),
        )
        station = cur.fetchone()

        ended_at = datetime.now()
        hours = Decimal((ended_at - b["started_at"]).total_seconds()) / Decimal(3600)
        if station["pricing_type"] == "per_kwh":
            total = money(body.energy_kwh * station["price_rate"])
        else:
            total = money(hours * station["price_rate"])
        fee = money(total * PLATFORM_FEE_PERCENT / 100)
        payout = total - fee

        cur.execute(
            """UPDATE bookings SET status='completed', ended_at=%s, energy_consumed=%s
               WHERE id=%s""",
            (ended_at, body.energy_kwh, booking_id),
        )
        tx_id = new_id()
        cur.execute(
            """INSERT INTO transactions
               (id, booking_id, total_amount, platform_fee, host_payout)
               VALUES (%s,%s,%s,%s,%s)""",
            (tx_id, booking_id, total, fee, payout),
        )
    return {
        "booking_id": booking_id,
        "status": "completed",
        "transaction_id": tx_id,
        "total_amount": total,
        "platform_fee": fee,
        "host_payout": payout,
    }


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------
@app.get("/transactions/mine", tags=["transactions"])
def my_transactions(user: dict = Depends(current_user), conn=Depends(get_db)):
    """Consumers see what they paid; hosts see earnings at their stations."""
    where = "s.host_id = %s" if user["role"] == "host" else "b.consumer_id = %s"
    with cursor(conn) as cur:
        cur.execute(
            f"""SELECT tr.*, b.station_id, s.title AS station_title, b.energy_consumed
                FROM transactions tr
                JOIN bookings b ON b.id = tr.booking_id
                JOIN charging_stations s ON s.id = b.station_id
                WHERE {where} ORDER BY tr.updated_at DESC""",
            (user["id"],),
        )
        return cur.fetchall()


@app.post("/transactions/{transaction_id}/pay", tags=["transactions"])
def pay_transaction(
    transaction_id: str,
    consumer: dict = Depends(require_role("consumer")),
    conn=Depends(get_db),
):
    """Mock payment. Replace the body with a real gateway call (Razorpay, Stripe...)."""
    with cursor(conn) as cur:
        cur.execute(
            """SELECT tr.id, tr.status, b.consumer_id FROM transactions tr
               JOIN bookings b ON b.id = tr.booking_id WHERE tr.id=%s FOR UPDATE""",
            (transaction_id,),
        )
        tr = cur.fetchone()
        if not tr:
            raise HTTPException(404, "Transaction not found")
        if tr["consumer_id"] != consumer["id"]:
            raise HTTPException(403, "Not your transaction")
        if tr["status"] == "completed":
            raise HTTPException(409, "Already paid")
        cur.execute("UPDATE transactions SET status='completed' WHERE id=%s", (transaction_id,))
    return {"id": transaction_id, "status": "completed"}


@app.get("/health", tags=["meta"])
def health(conn=Depends(get_db)):
    with cursor(conn) as cur:
        cur.execute("SELECT 1 AS ok")
        return cur.fetchone()
