"""
Generate signed QR codes for party guests.

Usage:
    export HMAC_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
    python generate_qrs.py guests.csv qrs/

Input CSV columns:
    name        (required) display name for the invite (e.g. "Hassan Family" or "John & Mary")
    id          (optional) stable invite ID — auto-generated if missing
    party_size  (optional) how many people this invite admits — default 1
    table       (optional) table label (number, name, anything) — blank means no table shown

Output:
    qrs/<name>_<id>.png   one QR per invite (the whole family shares one code)
    qrs/_seed.csv         guest list for backend seeding
"""
import csv
import hmac
import hashlib
import base64
import secrets
import os
import sys
from pathlib import Path

import qrcode


SECRET = os.environ.get("HMAC_SECRET", "").encode()


def sign(guest_id: str) -> str:
    """HMAC-SHA256, truncated to 12 bytes (96 bits) and base64url encoded."""
    digest = hmac.new(SECRET, guest_id.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:12]).decode().rstrip("=")


def parse_party_size(raw: str) -> int:
    raw = (raw or "").strip()
    if not raw:
        return 1
    try:
        n = int(raw)
    except ValueError:
        raise ValueError(f"party_size must be an integer, got {raw!r}")
    if n < 1:
        raise ValueError(f"party_size must be >= 1, got {n}")
    return n


def main(csv_path: str, output_dir: str) -> None:
    if not SECRET:
        print("ERROR: HMAC_SECRET env var not set.")
        print("Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(32))')")
        sys.exit(1)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    with open(csv_path) as f:
        guests = list(csv.DictReader(f))

    if not guests or "name" not in guests[0]:
        print("ERROR: CSV must have a 'name' column.")
        sys.exit(1)

    seed_rows = []
    for guest in guests:
        gid = (guest.get("id") or "").strip() or secrets.token_urlsafe(8)
        name = guest["name"].strip()
        party_size = parse_party_size(guest.get("party_size", ""))
        table = (guest.get("table") or "").strip()

        sig = sign(gid)
        payload = f"{gid}|{sig}"

        qr = qrcode.QRCode(box_size=10, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(payload)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        safe_name = "".join(c if c.isalnum() else "_" for c in name)
        filename = f"{safe_name}_{gid}.png"
        img.save(out / filename)

        seed_rows.append({
            "id": gid,
            "name": name,
            "party_size": party_size,
            "table": table,
        })
        suffix = f"  (party of {party_size})" if party_size > 1 else ""
        print(f"  {name:<30} -> {filename}{suffix}")

    seed_path = out / "_seed.csv"
    with open(seed_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "name", "party_size", "table"])
        writer.writeheader()
        writer.writerows(seed_rows)

    total_people = sum(r["party_size"] for r in seed_rows)
    print(f"\nDone. {len(guests)} QR codes for {total_people} people written to {out}/")
    print(f"Seed file for backend: {seed_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python generate_qrs.py <guests.csv> <output_dir>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
