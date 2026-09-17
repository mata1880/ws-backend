#!/usr/bin/env python3
"""
Weiss Schwarz official card catalog scraper (ws-tcg.com)
==========================================================

Pulls full card details (name, rarity, stats, image, expansion) from the
official Bushiroad card database — a real JSON API, not scraped HTML, so
this is far more reliable than the yuyu-tei price scraper.

This is a SEPARATE data source from yuyu-tei:
  - ws-tcg.com (this script)  -> card details for browsing: name, rarity,
    stats, card text, image. No prices.
  - yuyu-tei.jp (yuyutei_scraper.py) -> sell/buylist prices only.

The collection page matches the two up by cardNumber (e.g. "OSK/S133-001").

USAGE
-----
    python wstcg_scraper.py --query osk --site
    python wstcg_scraper.py --query 推しの子 --out osk_catalog.json

--query matches whatever you'd type into the site's own search box
(title_number like "OSK", part of a card name, etc.) — same box you used
in your browser to find the searchJson request in the first place.
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional

import requests

BASE_URL = "https://ws-tcg.com"
SEARCH_PAGE_URL = f"{BASE_URL}/cardlist/search/"
SEARCH_JSON_URL = f"{BASE_URL}/manage/CardListUser/searchJson"
IMAGE_BASE_URL = f"{BASE_URL}/wordpress/wp-content/images/cardlist/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    "X-Requested-With": "XMLHttpRequest",
}


@dataclass
class CatalogCard:
    cardNumber: str
    name: str
    nameKana: Optional[str] = None
    rarity: Optional[str] = None
    titleNumber: Optional[str] = None
    expansionId: Optional[int] = None
    expansionName: Optional[str] = None
    color: Optional[str] = None
    level: Optional[str] = None
    cost: Optional[str] = None
    power: Optional[str] = None
    soul: Optional[str] = None
    trigger: Optional[str] = None
    traits: list = field(default_factory=list)
    text: Optional[str] = None
    flavor: Optional[str] = None
    imageUrl: Optional[str] = None
    scrapedAt: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def clean(v):
    """ws-tcg.com uses "-" as a null placeholder in a lot of fields."""
    if v is None:
        return None
    v = str(v).strip()
    return None if v in ("", "-") else v


def clean_soul(v):
    # soul comes back as the icon markup repeated once per point, e.g.
    # "[[soul.gif]][[soul.gif]]" for soul 2 — count the icons instead.
    v = clean(v)
    if not v:
        return "0"
    return str(len(re.findall(r"soul\.gif", v)))


def clean_color(v):
    # color comes back like "[[blue.gif]]" — pull just "blue"
    v = clean(v)
    if not v:
        return None
    m = re.search(r"\[\[?([a-zA-Z]+)", v)
    return m.group(1) if m else v


FILTER_OPTIONS_URL = f"{BASE_URL}/manage/CardListUser/filter-options"
EXPANSIONS_CACHE_FILE = "wstcg_expansions_cache.json"


def fetch_expansion_names(session: requests.Session, force_refresh: bool = False) -> dict:
    """
    Fetches the official id -> name mapping for every Weiss Schwarz
    expansion ever released, straight from ws-tcg.com's own filter-options
    endpoint. Cached to a local file after the first fetch, so this only
    hits the site once (delete wstcg_expansions_cache.json, or pass
    force_refresh=True, to pick up newly announced sets).
    """
    if not force_refresh and os.path.exists(EXPANSIONS_CACHE_FILE):
        try:
            with open(EXPANSIONS_CACHE_FILE, encoding="utf-8") as f:
                cached = json.load(f)
            if cached:
                return {int(k): v for k, v in cached.items()}
        except (json.JSONDecodeError, ValueError, OSError):
            pass

    headers = dict(HEADERS, Referer=SEARCH_PAGE_URL)
    resp = session.get(FILTER_OPTIONS_URL, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    names = {}
    for exp in data.get("expansions", []):
        eid, name = exp.get("id"), clean(exp.get("name"))
        if eid is not None and name:
            names[int(eid)] = name

    try:
        with open(EXPANSIONS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in names.items()}, f, ensure_ascii=False, indent=2)
        print(f"  (cached {len(names)} expansion names -> {EXPANSIONS_CACHE_FILE})")
    except OSError:
        pass

    return names


def parse_card(raw: dict, expansion_names: dict = None) -> CatalogCard:
    traits = [t for t in (clean(raw.get("feature1")), clean(raw.get("feature2")), clean(raw.get("feature3"))) if t]
    picture = clean(raw.get("picture"))
    expansion_id = raw.get("expansion")
    try:
        expansion_id = int(expansion_id) if expansion_id not in (None, "") else None
    except (TypeError, ValueError):
        expansion_id = None

    # The expansion's real name comes bundled with each card as a nested
    # object — no separate lookup needed.
    expansion_rel = raw.get("expansion_rel") or {}
    expansion_name = clean(expansion_rel.get("name")) or (expansion_names or {}).get(expansion_id)

    return CatalogCard(
        cardNumber=clean(raw.get("card_number")) or "",
        name=clean(raw.get("card_name")) or "",
        nameKana=clean(raw.get("card_name_kana")),
        rarity=clean(raw.get("rare")),
        titleNumber=clean(raw.get("title_number")),
        expansionId=expansion_id,
        expansionName=expansion_name,
        color=clean_color(raw.get("color")),
        level=clean(raw.get("level")),
        cost=clean(raw.get("cost")),
        power=clean(raw.get("power")),
        soul=clean_soul(raw.get("soul")),
        trigger=clean(raw.get("card_trigger")),
        traits=traits,
        text=clean(raw.get("text")),
        flavor=clean(raw.get("flavor")),
        imageUrl=(IMAGE_BASE_URL + picture) if picture else None,
    )


def fetch_all(session: requests.Session, query: str, delay: float = 1.0, page_size: int = 30, debug: bool = False):
    # Visiting the search page first picks up the cookies (csrfToken etc.)
    # the JSON endpoint expects, same as a real browser would.
    session.get(SEARCH_PAGE_URL, headers=HEADERS, params={"keyword": query}, timeout=30)
    time.sleep(delay)

    expansion_names = fetch_expansion_names(session)
    time.sleep(delay)

    params = {
        "keyword": query,
        "keyword_or": "",
        "keyword_not": "",
        "keyword_type[]": "all",
        "title_number": "",
        "expansion": "",
        "card_kind": "",
        "level_s": "", "level_e": "",
        "power_s": "", "power_e": "",
        "color": "",
        "soul_s": "", "soul_e": "",
        "cost_s": "", "cost_e": "",
        "trigger": "",
        "option_counter": "0",
        "option_clock": "0",
        "parallel": "0",
        "show_page_count": str(page_size),
        "page": "1",
    }

    all_cards = []
    page = 1
    total = None
    page_count = None

    while True:
        params["page"] = str(page)
        headers = dict(HEADERS, Referer=SEARCH_PAGE_URL + "?keyword=" + query)
        resp = session.get(SEARCH_JSON_URL, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if debug and page == 1:
            with open("debug_wstcg_response.json", "w", encoding="utf-8") as f:
                f.write(resp.text)
            print("[debug] saved raw response -> debug_wstcg_response.json")

        items = data.get("items", [])
        total = data.get("total", total)
        page_count = data.get("page_count", page_count)

        print(f"  page {page}/{page_count or '?'} — {len(items)} cards (total {total})")
        all_cards.extend(parse_card(item, expansion_names) for item in items)

        if not items or (page_count and page >= page_count):
            break
        page += 1
        time.sleep(delay)

    return all_cards, total


def update_site_catalog(cards, query: str, site_dir: str = "docs"):
    data_dir = os.path.join(site_dir, "data")
    os.makedirs(data_dir, exist_ok=True)

    safe_query = re.sub(r"[^a-zA-Z0-9_\-]+", "_", query.strip().lower()) or "query"
    fname = f"catalog_{safe_query}.json"
    fpath = os.path.join(data_dir, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump([asdict(c) for c in cards], f, ensure_ascii=False, indent=2)

    manifest_path = os.path.join(data_dir, "catalog_manifest.json")
    manifest = []
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            try:
                manifest = json.load(f)
            except json.JSONDecodeError:
                manifest = []
    manifest = [m for m in manifest if m.get("query") != query]
    manifest.append({
        "query": query,
        "file": f"data/{fname}",
        "count": len(cards),
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
    })
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"Updated catalog -> {fpath} (+ catalog_manifest.json)")


def write_output(cards, out_path: str):
    if not cards:
        print("No cards found — nothing written.")
        return
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump([asdict(c) for c in cards], f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(cards)} cards -> {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Scrape the official ws-tcg.com card catalog.")
    parser.add_argument("--query", required=True, help='Search term, e.g. "OSK" or a card/series name')
    parser.add_argument("--out", default="catalog.json", help="Output JSON file")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests")
    parser.add_argument("--site", action="store_true", help="Also write into docs/data/ for the collection page")
    parser.add_argument("--site-dir", default="docs")
    parser.add_argument("--debug", action="store_true", help="Save the raw first-page JSON response to debug_wstcg_response.json")
    args = parser.parse_args()

    session = requests.Session()
    print(f'Searching ws-tcg.com for "{args.query}"...')
    try:
        cards, total = fetch_all(session, args.query, args.delay, debug=args.debug)
    except requests.RequestException as e:
        print(f"[error] Network error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"[error] Couldn't parse the response as JSON — the API may have changed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\n{len(cards)} card(s) retrieved (site reports {total} total matches)")

    if args.site:
        update_site_catalog(cards, args.query, args.site_dir)

    write_output(cards, args.out)


if __name__ == "__main__":
    main()
