# Deploy & Operate

Reference for putting the QR party system on Render so you can run the whole event from your phone, plus notes for future improvements.

---

## Prerequisites

- GitHub repo (already set up: `github.com/alialdadah/qr-party`)
- Render account (free): https://render.com — sign up with GitHub
- Your `HMAC_SECRET` from local `.env` — **never lose this**. Every QR you generate is signed with it. If it changes, every QR breaks.

## Privacy heads-up

Check whether your GitHub repo is **public** or **private**. If public, the committed `seed.csv` and `guests.csv` expose every guest's name, party size, and table to the world. Make it private if those names should stay private:

```powershell
gh repo edit alialdadah/qr-party --visibility private
```

…or via Settings → General → Danger Zone → Change visibility on github.com.

---

## First-time deploy

### 1. Commit and push

```powershell
git add .gitignore render.yaml requirements.txt main.py scanner.html generate_qrs.py guests.csv seed.csv load-env.ps1 README.md DEPLOY.md
git commit -m "Render deploy config"
git push origin main
```

`.env`, `party.db`, and `qrs/` stay out of git (gitignored).

### 2. Create the Blueprint on Render

1. Render dashboard → **New → Blueprint**
2. Connect the `qr-party` repo
3. Render reads `render.yaml`, prompts for the two `sync: false` env vars:

| Key | Value |
|---|---|
| `HMAC_SECRET` | Exact value from your local `.env`. |
| `STAFF_PIN` | `4729` (or whatever you prefer) |

4. Click **Apply** / **Create Resources**

### 3. Wait for deploy (~3–5 min)

Watch logs in the dashboard. Success: `Uvicorn running on http://0.0.0.0:10000`. URL appears at top: `https://qr-party-XXXX.onrender.com`. Copy it.

### 4. Seed the database (one-shot)

```powershell
curl.exe -X POST https://qr-party-XXXX.onrender.com/seed -H "X-Staff-Pin: 4729"
```

Expect: `{"added":8,"updated":0,"total":8}`.

### 5. Bookmark the URL on your phone

That's the whole system. Your laptop is no longer needed.

---

## Day-of operations

| Action | How |
|---|---|
| Wake the server before doors open | Hit the URL from any phone ~1 min ahead — cold start ~30s |
| Scan a guest | Open URL on phone, PIN already remembered, tap Resume scanning |
| Check live stats | `https://...onrender.com/stats` with `X-Staff-Pin` header (or hit from another phone) |
| Add a walk-in | Export → edit → import (see below), or DB Browser if you're at the laptop |
| Un-admit a mis-scan | Same — set `admitted_count` back to 0 in Excel, re-upload |

---

## Updating guests

### Quick fixes (edit live data)

From any laptop with internet:

```powershell
# Download
curl.exe https://qr-party-XXXX.onrender.com/export -H "X-Staff-Pin: 4729" -o guests-export.csv
start guests-export.csv      # edit in Excel

# Upload
curl.exe -X POST https://qr-party-XXXX.onrender.com/import `
  -H "X-Staff-Pin: 4729" `
  -F "file=@guests-export.csv"
```

Editable columns: `name`, `party_size`, `admitted_count`, `table`, `scanned_at`, `scanned_by`. **Never change `id`.** Save as **CSV UTF-8** in Excel.

### Adding guests who need a QR

If a new guest needs a QR sent ahead of time:

```powershell
# 1. Add row to guests.csv
# 2. Regenerate QRs (your local HMAC_SECRET must still match Render's)
.\load-env.ps1
python generate_qrs.py guests.csv qrs/
Copy-Item qrs/_seed.csv seed.csv -Force

# 3. Commit and push — Render auto-redeploys
git add guests.csv seed.csv
git commit -m "Add guest X"
git push origin main

# 4. Re-seed (upsert; existing scans preserved)
curl.exe -X POST https://qr-party-XXXX.onrender.com/seed -H "X-Staff-Pin: 4729"

# 5. Email the new PNG from qrs/ to the guest
```

---

## Free tier limits to know

| Limit | Impact | Mitigation |
|---|---|---|
| Web service sleeps after 15 min idle | First scan of the night takes ~30s | Pre-warm with `/healthz`, or pay $7/mo Starter to disable sleep |
| Postgres free tier expires after 90 days | DB is dropped — guests gone | For one-night event, irrelevant. For long-term: $7/mo Postgres |
| Build minutes capped at 500/mo | Hit only if you push 100s of times | Squash trivial commits before pushing |
| Bandwidth 100GB/mo | Won't hit for a party | n/a |

For an actual event night, consider:
- Sign up for **UptimeRobot** (free) and have it ping `/healthz` every 5 min on event day so the server stays warm.
- Or: temporarily upgrade to **Starter** ($7) the morning of, downgrade after.

---

## Future development ideas

Not needed for v1 but worth knowing:

### UX improvements
- **Mobile shortcut for /import**: Use [HTTP Shortcuts](https://http-shortcuts.rmy.ch/) (Android) or Apple Shortcuts (iOS) to wrap export → edit → import in one tap.
- **Walk-in flow on the scanner**: Add a button on `scanner.html` to manually add a guest without a QR (calls a new `/walkin` endpoint that mints an ID, marks `admitted_count = party_size`).
- **Multiple staff PINs**: Replace single `STAFF_PIN` with a small `staff` table (PIN → name). Lets you attribute scans accurately and revoke a PIN if a phone is lost.
- **Stats page**: A `/dashboard` HTML page showing live counts, recent scans, top tables. Currently you have to curl `/stats` and `/log`.
- **Per-table view**: `/guests?table=7` for "who's at table 7."

### Operational
- **Automatic backups**: Cron a `/export` download to S3 or Google Drive nightly. Or have Render Postgres back up to a managed service.
- **Email QRs automatically**: Wire `generate_qrs.py` into SendGrid / Mailgun so each new guest in `guests.csv` gets their PNG attached and emailed. Currently you email them by hand.
- **Print sheets**: A script that takes `qrs/` and lays out 6/8/12 QRs per A4 page with names, for the door staff's paper backup.
- **Audit log**: Right now `/log` shows only first-scan timestamps. Store every admit attempt (including duplicates and rejections) in a separate table for post-event analysis.

### Security
- **Rate-limit /verify**: `cloudflare` or `slowapi` middleware to stop someone brute-forcing the PIN. For a one-night event with a 4-digit PIN and a public URL, this is real.
- **Rotate the PIN at midnight** so a leaked screenshot from earlier doesn't compromise the rest of the night.
- **Audit who's looking at /guests** so you know if door staff are downloading the full guest list.

### Architecture
- **Move scanner.html to a CDN** if Render cold starts annoy you — static page loads instantly, only `/verify` etc. need the warm server.
- **WebSocket for live stats** so the scanner shows a running counter without polling.
- **Multi-event support**: scope guests / scans by an `event_id` so you can reuse the same deploy for multiple parties.

---

## Useful endpoints reference

All require `X-Staff-Pin` header (except `/` which is the scanner page and `/healthz` which is open).

| Method | Path | What |
|---|---|---|
| GET | `/` | Scanner PWA |
| GET | `/healthz` | Health check (open, no auth) |
| POST | `/verify` | Scan a QR. `admit_count: null` = preview, integer = admit that many |
| GET | `/stats` | Counts (people and invites) |
| GET | `/guests` | Full roster. `?status=none\|partial\|complete` to filter |
| GET | `/log?limit=N` | Recent scans, most recent first |
| POST | `/seed` | Upsert from `seed.csv` |
| GET | `/export` | Download current DB state as CSV |
| POST | `/import` | Upload edited CSV (multipart) |

---

## Local development (recap)

Three terminals from `C:\Users\alial\Desktop\github-projects\qr-party`:

```powershell
# Terminal 1 — backend
.\.venv\Scripts\Activate.ps1
.\load-env.ps1
uvicorn main:app --reload

# Terminal 2 — tunnel (for phone testing without deploying)
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://localhost:8000

# Terminal 3 — seed + queries
curl.exe -X POST http://127.0.0.1:8000/seed -H "X-Staff-Pin: 4729"
```
