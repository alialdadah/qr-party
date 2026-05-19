# Party QR Auth

A signed-QR check-in system with one-time-use enforcement. Three pieces:
1. `generate_qrs.py` builds tamper-proof QR codes from a guest CSV
2. `main.py` is the FastAPI backend that verifies and tracks scans
3. `scanner.html` is the PWA door staff opens on their phone

## How it works

Each QR encodes `guest_id|signature` where signature is `HMAC-SHA256(SECRET, guest_id)` truncated to 96 bits. The server has the secret, so it can verify a code is real without storing the signature. The DB tracks how many of each invite's party have been admitted, so duplicates and partials both get caught.

One QR per **invitation**, not per person — a family of four shares one QR, the server admits up to four people against it, and the door staff can split arrivals across multiple scans (e.g. 3 now, 1 later).

## Day 1: Generate QRs and deploy backend

### 1. Set up locally

```bash
pip install -r requirements.txt

export HMAC_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
export STAFF_PIN=4729   # whatever you want
echo "HMAC_SECRET=$HMAC_SECRET"   # save this somewhere safe
```

### 2. Prepare your guest list

Create `guests.csv` with at minimum a `name` column. Optionally add `party_size` (how many people that invite admits, default 1) and `table` (any string — number, name, whatever you write on the seating chart):

```csv
name,party_size,table
Sarah Ahmed,1,3
Hassan Family,4,7
John Smith,2,
Layla Hassan,1,Head
```

Leaving `table` blank just means no table is shown on the scanner for that guest.

### 3. Generate QR codes

```bash
python generate_qrs.py guests.csv qrs/
```

You get one PNG per guest in `qrs/` plus `qrs/_seed.csv` for the backend. Email or print the PNGs.

### 4. Deploy backend

Easiest free option is Render or Railway. Push these files to a GitHub repo with this layout:

```
/main.py
/requirements.txt
/seed.csv               (copied from qrs/_seed.csv)
```

On Render:
- New Web Service, connect repo
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Env vars: `HMAC_SECRET`, `STAFF_PIN` (same values as local)
- Add a persistent disk if you want the SQLite file to survive restarts, or use Postgres via `DATABASE_URL`

After it deploys, seed the guest table:

```bash
curl -X POST https://your-app.onrender.com/seed -H "X-Staff-Pin: 4729"
```

## Day 2: Scanner page and dry run

### 1. Host scanner.html

Drop `scanner.html` on Vercel, Netlify, or even GitHub Pages. The camera API only works on HTTPS, which all of these give you for free.

### 2. Test end to end

- Open the scanner URL on your phone
- Enter the backend URL and staff PIN
- Scan a solo guest's QR — should show green with name + table
- Scan a family QR — should show a blue confirm card with the family name, table, and a +/− stepper defaulted to the full party size. Tap Admit to let everyone in, or step down to admit fewer
- Scan a family QR again after a partial admit — confirm card shows fewer remaining; admit the rest
- Scan a fully-admitted QR — yellow with "X of X already admitted"
- Manually tamper with a QR (edit any character of the payload) and scan — should show red

### 3. Day-of checklist

- Charge all scanner phones
- Confirm venue wifi works on the scanner phones
- Print a backup paper list of guests (with table numbers) in case the backend goes down
- Brief door staff:
  - green = let in, send them to the table shown
  - blue = confirm head count, tap Admit (default = whole party)
  - yellow = already came in (verify identity before deciding)
  - red = not on the list / forged QR

## Useful endpoints during the party

- `GET /stats` shows live arrival count
- `GET /log` shows recent scans (most recent first)

Both need the `X-Staff-Pin` header.

## Security notes

- The HMAC secret must stay private. If it leaks, anyone can forge QRs.
- The 12-byte signature gives 96 bits of security, plenty for a one-night event.
- Staff PIN is the only auth on the backend. For a private party that's fine. Don't reuse it elsewhere.
- The SQLite DB on Render's ephemeral disk resets on redeploy. For redundancy use a managed Postgres or back up `party.db` periodically.
- Party size and table assignment live only in the database, never in the QR payload. A guest can't modify their QR to inflate their party or change tables.
- Upgrading from the original schema is automatic: on first boot the app runs `ALTER TABLE` to add the new columns (`party_size`, `admitted_count`, `table_number`) and backfills `admitted_count = 1` for any guest already scanned under the old code.