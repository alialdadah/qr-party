"""
Party QR verification backend.

Run locally:
    export HMAC_SECRET=...    # same secret used by generate_qrs.py
    export STAFF_PIN=4729     # door staff PIN
    uvicorn main:app --reload

Endpoints:
    POST /verify   verify a scanned QR; optionally admit N people from the invite
    GET  /stats    return total/scanned/remaining counts (counted in people)
    POST /seed     load guests from seed.csv (run once after deploy)
    GET  /log      recent scans (most recent first)
"""
import os
import csv
import io
import json
import hmac
import hashlib
import base64
import secrets
from datetime import datetime, timezone

import qrcode
from fastapi import FastAPI, HTTPException, Header, Depends, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, DateTime, Integer
from sqlalchemy.orm import declarative_base, sessionmaker, Session


SECRET = os.environ.get("HMAC_SECRET", "").encode()
STAFF_PIN = os.environ.get("STAFF_PIN", "")
ADMIN_PIN = os.environ.get("ADMIN_PIN", "")
DB_URL = os.environ.get("DATABASE_URL", "sqlite:///./party.db")
# Render hands out URLs starting with "postgres://"; SQLAlchemy 2.x requires "postgresql://".
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)
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
    party_size = Column(Integer, nullable=False, default=1)
    admitted_count = Column(Integer, nullable=False, default=0)
    table_number = Column(String, nullable=True)
    scanned_at = Column(DateTime, nullable=True)   # first admit
    scanned_by = Column(String, nullable=True)     # staff name on first admit


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, nullable=False)
    actor = Column(String, nullable=False)
    actor_role = Column(String, nullable=False)    # "staff" | "admin"
    action = Column(String, nullable=False)        # "admit" | "create" | "update" | "delete"
    guest_id = Column(String, nullable=True)
    guest_name = Column(String, nullable=True)
    details = Column(String, nullable=True)        # JSON


Base.metadata.create_all(engine)


def audit(db: Session, actor: str, role: str, action: str,
          guest: Guest | None, details: dict | None = None) -> None:
    db.add(AuditLog(
        ts=datetime.now(timezone.utc),
        actor=(actor or "unknown").strip() or "unknown",
        actor_role=role,
        action=action,
        guest_id=guest.id if guest else None,
        guest_name=guest.name if guest else None,
        details=json.dumps(details) if details else None,
    ))


def migrate_legacy_schema() -> None:
    """Add new columns to an old guests table if it predates party_size support."""
    if not DB_URL.startswith("sqlite"):
        return  # only auto-migrating SQLite; managed DBs should use real migrations
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(guests)").fetchall()}
        if "party_size" not in cols:
            conn.exec_driver_sql("ALTER TABLE guests ADD COLUMN party_size INTEGER NOT NULL DEFAULT 1")
        if "admitted_count" not in cols:
            conn.exec_driver_sql("ALTER TABLE guests ADD COLUMN admitted_count INTEGER NOT NULL DEFAULT 0")
            # Anyone already scanned under the old schema admitted exactly 1 person.
            conn.exec_driver_sql(
                "UPDATE guests SET admitted_count = 1 WHERE scanned_at IS NOT NULL AND admitted_count = 0"
            )
        if "table_number" not in cols:
            conn.exec_driver_sql("ALTER TABLE guests ADD COLUMN table_number TEXT")


migrate_legacy_schema()

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


def verify_pin(x_staff_pin: str = Header(default="")) -> str:
    """Accept either staff or admin PIN. Returns the role."""
    if hmac.compare_digest(x_staff_pin, STAFF_PIN):
        return "staff"
    if ADMIN_PIN and hmac.compare_digest(x_staff_pin, ADMIN_PIN):
        return "admin"
    raise HTTPException(status_code=401, detail="Invalid PIN")


def verify_admin(x_staff_pin: str = Header(default="")) -> str:
    """Admin-only endpoints. ADMIN_PIN must be configured."""
    if not ADMIN_PIN or not hmac.compare_digest(x_staff_pin, ADMIN_PIN):
        raise HTTPException(status_code=401, detail="Admin PIN required")
    return "admin"


def verify_signature(guest_id: str, sig: str) -> bool:
    expected_digest = hmac.new(SECRET, guest_id.encode(), hashlib.sha256).digest()
    expected_sig = base64.urlsafe_b64encode(expected_digest[:12]).decode().rstrip("=")
    return hmac.compare_digest(expected_sig, sig)


def sign_guest(guest_id: str) -> str:
    digest = hmac.new(SECRET, guest_id.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:12]).decode().rstrip("=")


def qr_png_bytes(guest_id: str) -> bytes:
    payload = f"{guest_id}|{sign_guest(guest_id)}"
    qr = qrcode.QRCode(box_size=10, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class VerifyRequest(BaseModel):
    payload: str
    scanner_name: str = "door"
    # How many people from this invite to admit on this scan.
    # None means "preview only": don't admit anyone, just return the guest's status
    # so the scanner can show a confirm card for families.
    admit_count: int | None = None


def _guest_payload(guest: Guest, status: str, extra: dict | None = None) -> dict:
    remaining = max(guest.party_size - guest.admitted_count, 0)
    body = {
        "status": status,
        "name": guest.name,
        "table": guest.table_number or "",
        "party_size": guest.party_size,
        "admitted_count": guest.admitted_count,
        "remaining": remaining,
    }
    if guest.scanned_at:
        body["scanned_at"] = guest.scanned_at.replace(tzinfo=timezone.utc).isoformat()
        body["scanned_by"] = guest.scanned_by
    if extra:
        body.update(extra)
    return body


@app.post("/verify")
def verify(req: VerifyRequest, db: Session = Depends(get_db), role: str = Depends(verify_pin)):
    try:
        guest_id, sig = req.payload.split("|", 1)
    except ValueError:
        return {"status": "invalid", "reason": "Malformed QR"}

    if not verify_signature(guest_id, sig):
        return {"status": "invalid", "reason": "Forged or tampered QR"}

    guest = db.query(Guest).filter(Guest.id == guest_id).first()
    if not guest:
        return {"status": "invalid", "reason": "Not on guest list"}

    remaining = guest.party_size - guest.admitted_count

    # Preview-only scan: scanner is asking "what is this invite?" before deciding
    # how many to admit. Used for families (party_size > 1).
    if req.admit_count is None:
        if remaining <= 0:
            return _guest_payload(guest, "duplicate")
        if guest.party_size == 1:
            # Solo invite — admit immediately, no confirm step needed.
            guest.admitted_count = 1
            if not guest.scanned_at:
                guest.scanned_at = datetime.now(timezone.utc)
                guest.scanned_by = req.scanner_name
            audit(db, req.scanner_name, role, "admit", guest, {"count": 1, "via": "scan"})
            db.commit()
            return _guest_payload(guest, "ok", {"admitted_now": 1})
        # Family — return a "confirm" status so the scanner shows the stepper.
        return _guest_payload(guest, "confirm")

    # Explicit admit request from the scanner.
    if req.admit_count < 1:
        return _guest_payload(guest, "invalid", {"reason": "admit_count must be >= 1"})
    if remaining <= 0:
        return _guest_payload(guest, "duplicate")
    if req.admit_count > remaining:
        return _guest_payload(
            guest, "invalid",
            {"reason": f"Only {remaining} of {guest.party_size} remaining"},
        )

    guest.admitted_count += req.admit_count
    if not guest.scanned_at:
        guest.scanned_at = datetime.now(timezone.utc)
        guest.scanned_by = req.scanner_name
    audit(db, req.scanner_name, role, "admit", guest, {"count": req.admit_count, "via": "scan"})
    db.commit()
    return _guest_payload(guest, "ok", {"admitted_now": req.admit_count})


@app.get("/stats", dependencies=[Depends(verify_pin)])
def stats(db: Session = Depends(get_db)):
    invites_total = db.query(Guest).count()
    invites_scanned = db.query(Guest).filter(Guest.admitted_count > 0).count()
    people_total = db.query(Guest).with_entities(Guest.party_size).all()
    people_admitted = db.query(Guest).with_entities(Guest.admitted_count).all()
    total = sum(row[0] for row in people_total)
    scanned = sum(row[0] for row in people_admitted)
    return {
        "total": total,
        "scanned": scanned,
        "remaining": total - scanned,
        "invites_total": invites_total,
        "invites_scanned": invites_scanned,
    }


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
            "table": r.table_number or "",
            "party_size": r.party_size,
            "admitted_count": r.admitted_count,
            "scanned_at": r.scanned_at.replace(tzinfo=timezone.utc).isoformat(),
            "scanned_by": r.scanned_by,
        }
        for r in rows
    ]


@app.get("/guests", dependencies=[Depends(verify_pin)])
def list_guests(status: str | None = None, db: Session = Depends(get_db)):
    """Full roster with admit status. Optional ?status=none|partial|complete filter."""
    rows = db.query(Guest).order_by(Guest.name).all()
    out = []
    for r in rows:
        if r.admitted_count == 0:
            s = "none"
        elif r.admitted_count >= r.party_size:
            s = "complete"
        else:
            s = "partial"
        if status and s != status:
            continue
        out.append({
            "id": r.id,
            "name": r.name,
            "table": r.table_number or "",
            "party_size": r.party_size,
            "admitted_count": r.admitted_count,
            "remaining": max(r.party_size - r.admitted_count, 0),
            "status": s,
            "scanned_at": r.scanned_at.replace(tzinfo=timezone.utc).isoformat() if r.scanned_at else None,
            "scanned_by": r.scanned_by,
        })
    return out


@app.post("/seed", dependencies=[Depends(verify_pin)])
def seed(db: Session = Depends(get_db)):
    """Load guest list from seed.csv. Safe to run multiple times.

    For existing guests: updates name/party_size/table (but never lowers
    party_size below admitted_count). For new guests: inserts a fresh row.
    """
    if not os.path.exists(SEED_PATH):
        raise HTTPException(404, f"{SEED_PATH} not found")

    added = updated = 0
    with open(SEED_PATH) as f:
        for row in csv.DictReader(f):
            gid = row["id"]
            name = row["name"]
            party_size = int(row.get("party_size") or 1)
            table = (row.get("table") or "").strip() or None

            guest = db.query(Guest).filter(Guest.id == gid).first()
            if guest is None:
                db.add(Guest(
                    id=gid, name=name,
                    party_size=party_size, admitted_count=0,
                    table_number=table,
                ))
                added += 1
            else:
                guest.name = name
                guest.party_size = max(party_size, guest.admitted_count)
                guest.table_number = table
                updated += 1
    db.commit()
    return {"added": added, "updated": updated, "total": db.query(Guest).count()}


EXPORT_FIELDS = ["id", "name", "party_size", "admitted_count", "table", "scanned_at", "scanned_by"]


@app.get("/export", dependencies=[Depends(verify_pin)])
def export_csv(db: Session = Depends(get_db)):
    """Download the live guest list as a CSV you can open in Excel."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS)
    writer.writeheader()
    for r in db.query(Guest).order_by(Guest.name).all():
        writer.writerow({
            "id": r.id,
            "name": r.name,
            "party_size": r.party_size,
            "admitted_count": r.admitted_count,
            "table": r.table_number or "",
            "scanned_at": r.scanned_at.replace(tzinfo=timezone.utc).isoformat() if r.scanned_at else "",
            "scanned_by": r.scanned_by or "",
        })
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="guests-export.csv"'},
    )


@app.post("/import", dependencies=[Depends(verify_pin)])
async def import_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload an edited CSV (from /export) to overwrite guest state.

    Match is by `id`. Editable: name, party_size, admitted_count, table,
    scanned_at, scanned_by. New rows allowed (need id + name; party_size
    defaults to 1). Rows missing id are skipped. admitted_count clamps
    to [0, party_size].
    """
    raw = (await file.read()).decode("utf-8-sig")  # strip Excel BOM if present
    reader = csv.DictReader(io.StringIO(raw))

    added = updated = skipped = 0
    errors: list[str] = []

    for i, row in enumerate(reader, start=2):  # row 2 == first data row
        gid = (row.get("id") or "").strip()
        name = (row.get("name") or "").strip()
        if not gid or not name:
            skipped += 1
            errors.append(f"row {i}: missing id or name")
            continue

        try:
            party_size = int(row.get("party_size") or 1)
            admitted_count = int(row.get("admitted_count") or 0)
        except ValueError:
            skipped += 1
            errors.append(f"row {i}: party_size/admitted_count must be integers")
            continue

        if party_size < 1:
            party_size = 1
        admitted_count = max(0, min(admitted_count, party_size))
        table = (row.get("table") or "").strip() or None

        scanned_at = None
        raw_ts = (row.get("scanned_at") or "").strip()
        if raw_ts:
            try:
                scanned_at = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            except ValueError:
                errors.append(f"row {i}: bad scanned_at (kept previous value)")
        # If they cleared admitted_count to 0 but left a scanned_at, drop the timestamp.
        if admitted_count == 0:
            scanned_at = None

        scanned_by = (row.get("scanned_by") or "").strip() or None
        if admitted_count == 0:
            scanned_by = None

        guest = db.query(Guest).filter(Guest.id == gid).first()
        if guest is None:
            db.add(Guest(
                id=gid, name=name,
                party_size=party_size,
                admitted_count=admitted_count,
                table_number=table,
                scanned_at=scanned_at,
                scanned_by=scanned_by,
            ))
            added += 1
        else:
            guest.name = name
            guest.party_size = party_size
            guest.admitted_count = admitted_count
            guest.table_number = table
            # Preserve existing timestamp if user didn't supply one and guest was already in.
            if raw_ts or admitted_count == 0:
                guest.scanned_at = scanned_at
            if scanned_by is not None or admitted_count == 0:
                guest.scanned_by = scanned_by
            updated += 1

    db.commit()
    return {"added": added, "updated": updated, "skipped": skipped, "errors": errors}


class CreateGuestRequest(BaseModel):
    name: str
    party_size: int = 1
    table: str | None = None


class UpdateGuestRequest(BaseModel):
    name: str | None = None
    party_size: int | None = None
    table: str | None = None
    admitted_count: int | None = None


class ManualAdmitRequest(BaseModel):
    count: int = 1
    scanner_name: str = "admin"


def get_actor(x_actor_name: str = Header(default="")) -> str:
    return (x_actor_name or "").strip() or "admin"


@app.get("/whoami")
def whoami(role: str = Depends(verify_pin)):
    return {"role": role}


@app.post("/guests", dependencies=[Depends(verify_admin)])
def create_guest(req: CreateGuestRequest, db: Session = Depends(get_db),
                 actor: str = Depends(get_actor)):
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    if req.party_size < 1:
        raise HTTPException(400, "party_size must be >= 1")
    table = (req.table or "").strip() or None

    gid = secrets.token_urlsafe(8)
    while db.query(Guest).filter(Guest.id == gid).first() is not None:
        gid = secrets.token_urlsafe(8)

    guest = Guest(
        id=gid, name=name,
        party_size=req.party_size, admitted_count=0,
        table_number=table,
    )
    db.add(guest)
    audit(db, actor, "admin", "create", guest,
          {"party_size": req.party_size, "table": table})
    db.commit()
    return _guest_payload(guest, "ok", {"id": gid})


@app.patch("/guests/{guest_id}", dependencies=[Depends(verify_admin)])
def update_guest(guest_id: str, req: UpdateGuestRequest, db: Session = Depends(get_db),
                 actor: str = Depends(get_actor)):
    guest = db.query(Guest).filter(Guest.id == guest_id).first()
    if not guest:
        raise HTTPException(404, "guest not found")

    # Snapshot for diff.
    before = {
        "name": guest.name,
        "party_size": guest.party_size,
        "table": guest.table_number,
        "admitted_count": guest.admitted_count,
    }

    if req.name is not None:
        new_name = req.name.strip()
        if not new_name:
            raise HTTPException(400, "name cannot be blank")
        guest.name = new_name
    if req.party_size is not None:
        if req.party_size < 1:
            raise HTTPException(400, "party_size must be >= 1")
        guest.party_size = req.party_size
    if req.table is not None:
        guest.table_number = req.table.strip() or None
    if req.admitted_count is not None:
        if req.admitted_count < 0:
            raise HTTPException(400, "admitted_count must be >= 0")
        guest.admitted_count = min(req.admitted_count, guest.party_size)
        if guest.admitted_count == 0:
            guest.scanned_at = None
            guest.scanned_by = None

    # Keep party_size >= admitted_count even if admin lowers it.
    if guest.party_size < guest.admitted_count:
        guest.party_size = guest.admitted_count

    after = {
        "name": guest.name,
        "party_size": guest.party_size,
        "table": guest.table_number,
        "admitted_count": guest.admitted_count,
    }
    changes = {k: {"from": before[k], "to": after[k]} for k in before if before[k] != after[k]}
    if changes:
        audit(db, actor, "admin", "update", guest, {"changes": changes})
    db.commit()
    return _guest_payload(guest, "ok", {"id": guest.id})


@app.delete("/guests/{guest_id}", dependencies=[Depends(verify_admin)])
def delete_guest(guest_id: str, db: Session = Depends(get_db),
                 actor: str = Depends(get_actor)):
    guest = db.query(Guest).filter(Guest.id == guest_id).first()
    if not guest:
        raise HTTPException(404, "guest not found")
    audit(db, actor, "admin", "delete", guest,
          {"party_size": guest.party_size, "admitted_count": guest.admitted_count})
    db.delete(guest)
    db.commit()
    return {"status": "ok", "id": guest_id}


@app.post("/guests/{guest_id}/admit", dependencies=[Depends(verify_admin)])
def manual_admit(guest_id: str, req: ManualAdmitRequest, db: Session = Depends(get_db)):
    """Admin override: admit N people from this invite without scanning a QR."""
    guest = db.query(Guest).filter(Guest.id == guest_id).first()
    if not guest:
        raise HTTPException(404, "guest not found")
    if req.count < 1:
        raise HTTPException(400, "count must be >= 1")

    remaining = guest.party_size - guest.admitted_count
    if remaining <= 0:
        return _guest_payload(guest, "duplicate")
    admit = min(req.count, remaining)

    guest.admitted_count += admit
    if not guest.scanned_at:
        guest.scanned_at = datetime.now(timezone.utc)
        guest.scanned_by = req.scanner_name
    audit(db, req.scanner_name, "admin", "admit", guest, {"count": admit, "via": "manual"})
    db.commit()
    return _guest_payload(guest, "ok", {"admitted_now": admit, "id": guest.id})


@app.get("/audit", dependencies=[Depends(verify_admin)])
def get_audit(limit: int = 200, action: str | None = None, actor: str | None = None,
              db: Session = Depends(get_db)):
    q = db.query(AuditLog).order_by(AuditLog.ts.desc())
    if action:
        q = q.filter(AuditLog.action == action)
    if actor:
        q = q.filter(AuditLog.actor.ilike(f"%{actor}%"))
    rows = q.limit(min(max(limit, 1), 1000)).all()
    return [
        {
            "id": r.id,
            "ts": r.ts.replace(tzinfo=timezone.utc).isoformat(),
            "actor": r.actor,
            "actor_role": r.actor_role,
            "action": r.action,
            "guest_id": r.guest_id,
            "guest_name": r.guest_name,
            "details": json.loads(r.details) if r.details else None,
        }
        for r in rows
    ]


@app.get("/guests/{guest_id}/qr", dependencies=[Depends(verify_admin)])
def guest_qr(guest_id: str, db: Session = Depends(get_db)):
    guest = db.query(Guest).filter(Guest.id == guest_id).first()
    if not guest:
        raise HTTPException(404, "guest not found")
    return Response(
        content=qr_png_bytes(guest_id),
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{guest_id}.png"'},
    )


@app.get("/")
def scanner_page():
    """Serve the scanner PWA so backend + scanner share one origin (one URL to tunnel)."""
    return FileResponse("scanner.html")


@app.get("/healthz")
def health():
    return {"status": "ok"}
