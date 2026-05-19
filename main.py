"""
Party QR verification backend.

Run locally:
    export HMAC_SECRET=...    # same secret used by generate_qrs.py
    export STAFF_PIN=4729     # door staff PIN
    uvicorn main:app --reload

Endpoints:
    POST /verify   verify a scanned QR payload, mark as used
    GET  /stats    return total/scanned/remaining counts
    POST /seed     load guests from seed.csv (run once after deploy)
    GET  /log      recent scans (most recent first)
"""
import os
import csv
import hmac
import hashlib
import base64
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Session


SECRET = os.environ.get("HMAC_SECRET", "").encode()
STAFF_PIN = os.environ.get("STAFF_PIN", "")
DB_URL = os.environ.get("DATABASE_URL", "sqlite:///./party.db")
SEED_PATH = os.environ.get("SEED_PATH", "seed.csv")

if not SECRET or not STAFF_PIN:
    raise RuntimeError("HMAC_SECRET and STAFF_PIN env vars are required.")

connect_args = {"check_same_thread": False} if DB_URL.startswith("sqlite") else {}
engine = create_engine(DB_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()


class Guest(Base):
    __tablename__ = "guests"
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    scanned_at = Column(DateTime, nullable=True)
    scanned_by = Column(String, nullable=True)


Base.metadata.create_all(engine)

app = FastAPI(title="Party QR Auth")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def verify_pin(x_staff_pin: str = Header(default="")):
    if not hmac.compare_digest(x_staff_pin, STAFF_PIN):
        raise HTTPException(status_code=401, detail="Invalid staff PIN")


def verify_signature(guest_id: str, sig: str) -> bool:
    expected_digest = hmac.new(SECRET, guest_id.encode(), hashlib.sha256).digest()
    expected_sig = base64.urlsafe_b64encode(expected_digest[:12]).decode().rstrip("=")
    return hmac.compare_digest(expected_sig, sig)


class VerifyRequest(BaseModel):
    payload: str
    scanner_name: str = "door"


@app.post("/verify", dependencies=[Depends(verify_pin)])
def verify(req: VerifyRequest, db: Session = Depends(get_db)):
    try:
        guest_id, sig = req.payload.split("|", 1)
    except ValueError:
        return {"status": "invalid", "reason": "Malformed QR"}

    if not verify_signature(guest_id, sig):
        return {"status": "invalid", "reason": "Forged or tampered QR"}

    guest = db.query(Guest).filter(Guest.id == guest_id).first()
    if not guest:
        return {"status": "invalid", "reason": "Not on guest list"}

    if guest.scanned_at:
        return {
            "status": "duplicate",
            "name": guest.name,
            "scanned_at": guest.scanned_at.replace(tzinfo=timezone.utc).isoformat(),
            "scanned_by": guest.scanned_by,
        }

    guest.scanned_at = datetime.now(timezone.utc)
    guest.scanned_by = req.scanner_name
    db.commit()

    return {"status": "ok", "name": guest.name}


@app.get("/stats", dependencies=[Depends(verify_pin)])
def stats(db: Session = Depends(get_db)):
    total = db.query(Guest).count()
    scanned = db.query(Guest).filter(Guest.scanned_at.isnot(None)).count()
    return {"total": total, "scanned": scanned, "remaining": total - scanned}


@app.get("/log", dependencies=[Depends(verify_pin)])
def recent_scans(limit: int = 20, db: Session = Depends(get_db)):
    rows = (
        db.query(Guest)
        .filter(Guest.scanned_at.isnot(None))
        .order_by(Guest.scanned_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "name": r.name,
            "scanned_at": r.scanned_at.replace(tzinfo=timezone.utc).isoformat(),
            "scanned_by": r.scanned_by,
        }
        for r in rows
    ]


@app.post("/seed", dependencies=[Depends(verify_pin)])
def seed(db: Session = Depends(get_db)):
    """Load guest list from seed.csv. Safe to run multiple times; only adds new guests."""
    if not os.path.exists(SEED_PATH):
        raise HTTPException(404, f"{SEED_PATH} not found")

    added = 0
    with open(SEED_PATH) as f:
        for row in csv.DictReader(f):
            if not db.query(Guest).filter(Guest.id == row["id"]).first():
                db.add(Guest(id=row["id"], name=row["name"]))
                added += 1
    db.commit()
    return {"added": added, "total": db.query(Guest).count()}


@app.get("/")
def health():
    return {"status": "ok"}
