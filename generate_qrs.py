"""
Generate signed QR codes for party guests.

Usage:
    export HMAC_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
    python generate_qrs.py guests.csv qrs/

Input CSV must have a 'name' column. An 'id' column is optional;
if missing, a random ID is generated per guest.

Output:
    qrs/<name>_<id>.png          one QR code per guest
    qrs/_seed.csv                guest list for backend seeding
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
        gid = guest.get("id") or secrets.token_urlsafe(8)
        sig = sign(gid)
        payload = f"{gid}|{sig}"

        qr = qrcode.QRCode(box_size=10, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(payload)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        safe_name = "".join(c if c.isalnum() else "_" for c in guest["name"])
        filename = f"{safe_name}_{gid}.png"
        img.save(out / filename)

        seed_rows.append({"id": gid, "name": guest["name"]})
        print(f"  {guest['name']:<30} -> {filename}")

    seed_path = out / "_seed.csv"
    with open(seed_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "name"])
        writer.writeheader()
        writer.writerows(seed_rows)

    print(f"\nDone. {len(guests)} codes written to {out}/")
    print(f"Seed file for backend: {seed_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python generate_qrs.py <guests.csv> <output_dir>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])