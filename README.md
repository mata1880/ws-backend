# Weiss Schwarz Collection — Backend

A small FastAPI + SQLite service that replaces the localStorage version of
the site: real collections, wishlists, binders, and per-copy tracking
(grade, frame, notes, purchase price), plus scrape-on-demand endpoints that
wrap the exact same `yuyutei_scraper.py` / `wstcg_scraper.py` logic that
already works — just writing into a database instead of JSON files.

## ⚠️ Honesty note on testing

I built this without internet access in my sandbox, so **I could not
actually install the dependencies and boot the server to confirm it runs**
— unlike the scraper files, which were tested against real captured data.
Every file passes a Python syntax check, and I reviewed the logic by hand,
but there could still be a runtime bug I can't catch without actually
running it. Follow "First run" below exactly, in order — if anything
breaks, send me the exact error and I'll fix it fast.

## Setup

```bash
cd ws-backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## First run — verify it actually works

```bash
uvicorn app.main:app --reload
```

If it starts without errors, open **http://localhost:8000/docs** in a
browser — FastAPI auto-generates an interactive page where you can try
every endpoint by clicking, no curl needed. Work through this order:

1. `GET /health` → should return `{"status": "ok"}`
2. `POST /scrape/prices` with body `{"card_code": "OSK/S133", "mode": "both"}`
   → this is the real test, since it's a live network call to yuyu-tei.
   Should return a summary like `{"cards_seen": 24, ...}`.
3. `GET /cards?search=OSK` → should list the cards you just scraped, with
   prices attached.
4. `POST /collections` with body `{"name": "Main"}` → creates your first
   collection.
5. `POST /copies` with `{"card_id": 1, "collection_id": 1}` → adds a copy
   of card #1 into it (use a real `id` from step 3's response).
6. `GET /collections/1/value` → should show `total_copies: 1` and a
   non-zero `total_sell_value_jpy`.

If all six work, the core loop (scrape → store → own → value) is confirmed
working end to end.

## What's here

- `app/models.py` — the data model. Read the big comment at the top of
  this file first — it explains the design (copies as the real unit of
  truth, price history instead of overwriting, etc.) and why each table
  looks the way it does.
- `app/scrape_bridge.py` — the only file that touches the scraper modules
  directly. Everything else talks to the database.
- `app/scraping/` — your existing `yuyutei_scraper.py` and
  `wstcg_scraper.py`, copied in unmodified.
- `app/routers/` — one file per resource (cards, collections, wishlists,
  binders, copies, scrape).

## What's built vs. what's still ahead

**Built:** the full data model, scrape-on-demand into price history,
collections with value calculation, wishlists with the exclusive/manual
membership rule, binders with layout-based frame compatibility and
priority auto-placement, per-copy grade/frame/note/purchase-price.

**Not built yet:** authentication (fine for personal use behind your own
deployment, not fine if this ever becomes multi-user), the actual
frontend talking to this API (the current `docs/` site still uses
localStorage — wiring it up to call this API instead is the next step),
and stacked-copy display logic (that's a frontend concern — the API
already exposes `GET /copies/by-card/{id}` for it).

## Deploying (Render / Railway)

Both offer a free tier that can run this + the SQLite file:

1. Push this folder to a GitHub repo (separate from — or a subfolder of —
   your existing `yuyutei-scraper` repo, your call).
2. On Render: New → Web Service → connect the repo → build command
   `pip install -r requirements.txt` → start command
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
3. First request after a period of inactivity will be slow (free tier
   "sleeps") — normal, not a bug.
4. The SQLite file lives on the server's disk. Free tiers usually wipe the
   disk on redeploy — if that happens to you, we'll want to add a Render
   persistent disk (small extra cost) before this holds real data you
   care about keeping.

## A note on CORS

`app/main.py` currently allows requests from any origin (`allow_origins:
["*"]`) so the frontend can reach it from anywhere during development.
Once the PWA has a real, fixed domain, tighten this to just that origin —
open CORS is fine for a solo personal project but no reason to leave it
wider than it needs to be once it's not just you testing locally.
