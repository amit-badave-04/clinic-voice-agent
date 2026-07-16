# Cliniko setup guide (one-time, ~45 min)

Cliniko's API cannot create practitioners or schedules (they are user accounts),
so parts of this are manual. Do steps 1–2 first; the seeder automates the rest
and tells you exactly what's still missing.

## 1. Create the trial account (5 min)

1. Go to https://www.cliniko.com → **Try Cliniko for free** (30 days, no card).
2. During setup choose **country: India** so currency is ₹ and the account
   timezone is **Asia/Kolkata** (critical — availability queries are interpreted
   in the account's local timezone).
3. Confirm under **Settings → Our clinic**: time zone `Asia/Kolkata`.

## 2. Create the API key (2 min)

1. Click your avatar (top right) → **My info** → **Manage API keys** → create key.
2. Your user must have **Administrator + Practitioner** roles (the default
   owner account has both) — otherwise the API returns 403 for scheduling calls.
3. Put the key in `.env` as `CLINIKO_API_KEY`. Its suffix (e.g. `-au1`) is the
   API shard — the code derives the base URL from it automatically.
4. Set `CLINIKO_VENDOR_NAME` / `CLINIKO_VENDOR_EMAIL` in `.env` (Cliniko blocks
   requests without an identifying User-Agent).

## 3. Run the seeder (creates branches + appointment types)

```
python -m seed.cliniko_seed
```

It creates the two businesses and four appointment types via API, then prints a
checklist of the practitioners you still need to add manually (step 4).

## 4. Add practitioners manually (~20 min)

For each practitioner the seeder lists (6 total — 4 is an acceptable minimum if
short on time; keep at least Dr. Pooja and Dr. Dilpreet since their split-branch
days power the cross-branch tests):

1. **Settings → Users & practitioners → Add user**.
   - Any unique email works — Gmail aliases are fine
     (e.g. `yourname+pooja@gmail.com`).
   - Tick **This user is a practitioner**.
2. Under the practitioner's settings, set **Show in online bookings: yes**.

## 5. Set appointment schedules per branch (~15 min)

For each practitioner: **Settings → Scheduling** (or the practitioner's
"Appointment schedule") → set their weekly hours **at the right business**,
Monday–Saturday (closed Sunday), exactly as printed by the seeder:

| Practitioner | Medax (Bannerghatta) | Arc (Wilson Garden) |
|---|---|---|
| Dr. Pooja Pandey Tripathi | 16:00–18:00 | 10:30–14:00 |
| Dr. Munesh Kumar Singh | 10:30–14:00 | 16:00–18:00 |
| Dr. Gopika Nair | 09:30–14:00, 15:00–18:00 | — |
| Dr. Anamika Lyngdoh | — | 11:00–14:00, 15:00–19:00 |
| Dr. Dilpreet Kaur | 09:30–12:30 | 15:00–17:00 |
| Dr. Netaji D | — | 10:00–14:00, 15:00–19:00 |

## 6. Enable online bookings everywhere (5 min — DO NOT SKIP)

`available_times` returns **nothing** unless business AND practitioner AND
appointment type are all online-booking enabled:

1. **Settings → Online bookings**: enable for both businesses.
2. Each practitioner: **Show in online bookings: yes** (step 4.2).
3. Each appointment type: **Show in online bookings: yes** (the seeder sets
   this via API; verify in Settings → Appointment types).
4. Make sure each appointment type is available for **all practitioners** and
   **both businesses** (checkboxes on the appointment-type page).

## 7. Re-run the seeder to link IDs

```
python -m seed.cliniko_seed     # now matches the practitioners you created
python -m seed.local_seed       # demo patients + policies
```

## 8. Smoke test

```
python -m scripts.smoke_cliniko   # prints tomorrow's open slots per practitioner
```

If every practitioner shows slots, Cliniko is ready.
