#!/usr/bin/env python3
"""
Yuyu-tei Scraper (local clone of lulzasaur/yuyutei-scraper on Apify)
=====================================================================

Scrapes yuyu-tei.jp (Japan's largest TCG singles shop) for sell prices
and/or buylist (kaitori) prices, by set code, and merges them into the
same record shape the Apify actor produces:

    {
      "game": "ws",
      "setCode": "osk3.0",
      "cardNumber": "OSK/S133-002SSP",
      "name": "Happy Spring Day MEMちょ(サイン入り)",
      "rarity": "SSP",
      "sellPriceJpy": 12800,
      "buyPriceJpy": 7000,
      "buyPriceBaseJpy": null,
      "buyPriceBoosted": false,
      "spreadJpy": 5800,
      "stock": 2,
      "condition": "near_mint",
      "availability": "In Stock",
      "imageUrl": "...",
      "url": "https://yuyu-tei.jp/sell/ws/card/osk3.0/10248",
      "scrapedAt": "2026-09-16T12:00:00Z"
    }

USAGE
-----
    pip install -r requirements.txt

    # Full set, sell + buylist merged (matches the screenshot table)
    python yuyutei_scraper.py --game ws --set osk3.0 --mode both --out cards.csv

    # Sell prices only
    python yuyutei_scraper.py --game poc --set sv08a --mode sell --out cards.json

    # Multiple sets in one run
    python yuyutei_scraper.py --game ygo --set wpp6 --set rota --mode both --out cards.csv

NOTES
-----
- This talks directly to yuyu-tei.jp's public pages. No login, no API key.
- Yuyu-tei is JP-only content; set codes come from the set's URL, e.g.
  https://yuyu-tei.jp/sell/poc/s/sv08a  ->  set code is "sv08a"
- Be polite: the script sleeps between requests (--delay, default 1.5s).
  Hammering the site can get your IP rate-limited or blocked.
- The parser is written to be resilient to markup tweaks (it anchors on
  each card's own detail-page link rather than guessing CSS class names),
  but retail sites do change their HTML. If a run comes back with 0 cards,
  re-run with --debug to dump the raw HTML of one page to debug_page.html
  and inspect it / send it back for a selector fix.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup

# =============================================================================
# EDIT THESE if you just want to hit "Run" in VS Code instead of using the
# command line. They're only used when you run the script with no --game/
# --set/etc. arguments; anything you type on the command line overrides them.
# =============================================================================
DEFAULT_GAME = "ws"              # locked to Weiss Schwarz
DEFAULT_SETS = ["osk3.0"]        # one or more set codes, e.g. ["osk3.0", "key20th"]
DEFAULT_MODE = "both"            # "sell", "buy", or "both"
DEFAULT_OUT = "cards.csv"        # output file, .csv or .json
DEFAULT_DELAY = 1.5              # seconds between requests
# =============================================================================

BASE_URL = "https://yuyu-tei.jp"
CARD_LINK_RE = re.compile(r"/(sell|buy)/([a-z0-9]+)/card/([^/]+)/(\d+)")
RARITY_HEADING_RE = re.compile(r"^([^\s]+)\s*Card List$")
YEN_RE = re.compile(r"([\d,]+)\s*円")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
}


@dataclass
class CardRecord:
    game: str
    setCode: str
    cardId: str
    cardNumber: str
    name: str
    rarity: Optional[str] = None
    condition: str = "near_mint"
    sellPriceJpy: Optional[int] = None
    buyPriceJpy: Optional[int] = None
    buyPriceBaseJpy: Optional[int] = None
    buyPriceBoosted: bool = False
    spreadJpy: Optional[int] = None
    stock: Optional[int] = None
    availability: Optional[str] = None
    imageUrl: Optional[str] = None
    url: Optional[str] = None
    scrapedAt: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def fetch(session: requests.Session, url: str, delay: float, debug: bool = False) -> str:
    resp = session.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    time.sleep(delay)
    if debug:
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(resp.text)
        print(f"[debug] saved raw HTML of {url} -> debug_page.html")
    return resp.text


def parse_yen(text: str) -> Optional[int]:
    m = YEN_RE.search(text)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def find_container(a_tag):
    """
    Walk up from a card's <a> link to the smallest ancestor block that
    also contains a yen price, which is a reliable proxy for "this is the
    whole product row/card, not the whole page".
    """
    node = a_tag
    for _ in range(6):  # don't walk up forever
        parent = node.parent
        if parent is None:
            break
        text = parent.get_text(" ", strip=True)
        if "円" in text and len(text) < 400:
            return parent
        node = parent
    return a_tag.parent or a_tag


def parse_listing_page(html: str, game: str, set_code: str, listing_type: str):
    """
    listing_type: "sell" or "buy"
    Returns (rows, set_name) — set_name is the human-readable set title,
    pulled from the page's <title> tag (e.g. "【推しの子】Vol.3").
    """
    soup = BeautifulSoup(html, "html.parser")

    set_name = None
    if soup.title and soup.title.string:
        set_name = soup.title.string.split("|")[0].split("｜")[0].strip() or None

    # yuyu-tei renders each card as <div class="card-product">...</div>.
    # This is confirmed against real page source (not guessed), so use it
    # as the primary path; fall back to a generic anchor-based heuristic
    # only if a page doesn't use this structure at all.
    if soup.select("div.card-product"):
        rows = _parse_card_product_tiles(soup, game, set_code, listing_type)
    else:
        rows = _parse_listing_page_generic(soup, game, set_code, listing_type)

    return rows, set_name


def _parse_card_product_tiles(soup: BeautifulSoup, game: str, set_code: str, listing_type: str):
    current_rarity = None
    seen_card_ids = set()
    rows = []

    for el in soup.descendants:
        if isinstance(el, str):
            m = RARITY_HEADING_RE.match(el.strip())
            if m:
                current_rarity = m.group(1)
            continue

        if el.name != "div" or "card-product" not in (el.get("class") or []):
            continue
        container = el

        link = container.find("a", href=CARD_LINK_RE)
        if not link:
            continue
        href = link.get("href", "")
        m = CARD_LINK_RE.search(href)
        if not m:
            continue
        _type, url_game, url_set, card_id = m.groups()
        key = (url_game, url_set, card_id)
        if key in seen_card_ids:
            continue
        seen_card_ids.add(key)

        row_set_code = url_set or set_code

        # Each tile has TWO <img> tags: a small favorite/star icon first,
        # then the real card image (whose alt text is "CODE RARITY Name").
        # Skip the star icon — that was the actual bug before this fix.
        img_tag = None
        for im in container.find_all("img"):
            alt = (im.get("alt") or "").strip()
            if alt and alt.lower() != "star":
                img_tag = im
                break
        alt_text = (img_tag.get("alt") or "").strip() if img_tag else ""

        block_text = container.get_text("\n", strip=True)
        lines = [l for l in block_text.split("\n") if l.strip()]

        card_number = None
        for l in lines:
            if re.match(r"^[A-Za-z0-9][A-Za-z0-9/._\-]{2,25}$", l) and "円" not in l:
                card_number = l
                break

        name = None
        rarity = current_rarity
        if card_number and alt_text.startswith(card_number):
            rest = alt_text[len(card_number):].strip()
            parts = rest.split(" ", 1)
            if len(parts) == 2 and len(parts[0]) <= 8:
                rarity = parts[0]
                name = parts[1]
            elif rest:
                name = rest
        if not name:
            candidates = [l for l in lines if "円" not in l and l != card_number]
            name = max(candidates, key=len) if candidates else (card_number or "")

        prices = [int(x.replace(",", "")) for x in YEN_RE.findall(block_text)]

        # Stock: yuyu-tei shows "×" (sold out), "◯" (plenty, no exact count),
        # or "N 点" (exact count) in a dedicated 在庫 (stock) label.
        stock = None
        availability = "In Stock"
        zaiko = container.find(class_=re.compile("zaiko"))
        if zaiko:
            zt = zaiko.get_text(" ", strip=True)
            if "×" in zt:
                stock = 0
                availability = "Sold Out"
            else:
                mnum = re.search(r"(\d+)\s*点", zt)
                if mnum:
                    stock = int(mnum.group(1))
        else:
            classes_str = " ".join(container.get("class") or []).lower()
            if "sold-out" in classes_str or "SOLD OUT" in block_text.upper() or "売り切れ" in block_text:
                stock = 0
                availability = "Sold Out"

        boosted = "PRICE UP" in block_text.upper()

        image_url = None
        if img_tag:
            image_url = img_tag.get("src") or img_tag.get("data-src")
            if image_url and image_url.startswith("//"):
                image_url = "https:" + image_url
            elif image_url and image_url.startswith("/"):
                image_url = BASE_URL + image_url

        rows.append(
            {
                "game": game,
                "setCode": row_set_code,
                "cardId": card_id,
                "cardNumber": card_number or "",
                "name": name or "",
                "rarity": rarity,
                "prices": prices,
                "boosted": boosted,
                "stock": stock,
                "availability": availability,
                "imageUrl": image_url,
                "url": BASE_URL + href if href.startswith("/") else href,
                "listing_type": listing_type,
            }
        )

    return rows


def _parse_listing_page_generic(soup: BeautifulSoup, game: str, set_code: str, listing_type: str):
    """Fallback for page types that don't use the div.card-product structure
    (unverified against real markup — used only if the primary parser finds
    nothing to work with)."""
    current_rarity = None
    seen_card_ids = set()
    rows = []

    for el in soup.descendants:
        if isinstance(el, str):
            stripped = el.strip()
            m = RARITY_HEADING_RE.match(stripped)
            if m:
                current_rarity = m.group(1)
            continue

        if el.name != "a":
            continue
        href = el.get("href", "")
        m = CARD_LINK_RE.search(href)
        if not m:
            continue

        _type, url_game, url_set, card_id = m.groups()
        key = (url_game, url_set, card_id)
        if key in seen_card_ids:
            continue

        container = find_container(el)
        block_text = container.get_text("\n", strip=True)
        lines = [l for l in block_text.split("\n") if l.strip()]

        row_set_code = url_set or set_code

        card_number = None
        for l in lines:
            if re.match(r"^[A-Za-z0-9][A-Za-z0-9/._\-]{2,25}$", l) and "円" not in l:
                card_number = l
                break

        img_tag = None
        for im in container.find_all("img"):
            alt = (im.get("alt") or "").strip()
            if alt and alt.lower() != "star":
                img_tag = im
                break
        alt_text = (img_tag.get("alt") or "").strip() if img_tag else ""

        source_text = None
        if card_number and alt_text.startswith(card_number):
            source_text = alt_text
        elif card_number:
            for l in lines:
                if l.startswith(card_number + " "):
                    source_text = l
                    break

        name = None
        rarity_from_text = None
        if source_text:
            rest = source_text[len(card_number):].strip()
            parts = rest.split(" ", 1)
            if len(parts) == 2 and len(parts[0]) <= 8:
                rarity_from_text = parts[0]
                name = parts[1]
            elif rest:
                name = rest
        if not name:
            candidates = [l for l in lines if "円" not in l and l != card_number]
            name = max(candidates, key=len) if candidates else (card_number or "")

        rarity = rarity_from_text or current_rarity

        prices = [int(x.replace(",", "")) for x in YEN_RE.findall(block_text)]

        sold_out = ("SOLD OUT" in block_text.upper()) or ("売り切れ" in block_text)
        has_qty_stepper = "- +" in block_text or re.search(r"[-−]\s*\+", block_text)
        if sold_out or not has_qty_stepper:
            stock = 0
            availability = "Sold Out"
        else:
            stock = None
            availability = "In Stock"

        boosted = "PRICE UP" in block_text.upper()

        image_url = None
        if img_tag:
            image_url = img_tag.get("src") or img_tag.get("data-src")
            if image_url and image_url.startswith("//"):
                image_url = "https:" + image_url
            elif image_url and image_url.startswith("/"):
                image_url = BASE_URL + image_url

        rows.append(
            {
                "game": game,
                "setCode": row_set_code,
                "cardId": card_id,
                "cardNumber": card_number or "",
                "name": name or "",
                "rarity": rarity,
                "prices": prices,
                "boosted": boosted,
                "stock": stock,
                "availability": availability,
                "imageUrl": image_url,
                "url": BASE_URL + href if href.startswith("/") else href,
                "listing_type": listing_type,
            }
        )
        seen_card_ids.add(key)

    return rows


def lookup_set_name(session: requests.Session, game: str, set_code: str, delay: float) -> Optional[str]:
    """Quick single-request check: fetch the sell listing page and return
    just the set's real name, without scraping any cards. Use this before
    a full scrape to confirm a code points at the set you think it does."""
    url = f"{BASE_URL}/sell/{game}/s/{set_code}"
    html = fetch(session, url, delay)
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        name = soup.title.string.split("|")[0].split("｜")[0].strip()
        return name or None
    return None


def scrape_set(session: requests.Session, game: str, set_code: str, mode: str,
                delay: float, debug: bool = False):
    sell_rows, buy_rows = [], []
    set_name = None

    if mode in ("sell", "both"):
        url = f"{BASE_URL}/sell/{game}/s/{set_code}"
        print(f"Fetching sell listing: {url}")
        html = fetch(session, url, delay, debug)
        sell_rows, set_name = parse_listing_page(html, game, set_code, "sell")
        before = len(sell_rows)
        sell_rows = [r for r in sell_rows if r["setCode"].lower() == set_code.lower()]
        dropped = before - len(sell_rows)
        print(f"  -> parsed {len(sell_rows)} sell rows" + (f' ("{set_name}")' if set_name else "")
              + (f"  [dropped {dropped} unrelated/pickup cards]" if dropped else ""))

    if mode in ("buy", "both"):
        url = f"{BASE_URL}/buy/{game}/s/{set_code}"
        print(f"Fetching buylist: {url}")
        html = fetch(session, url, delay, debug)
        buy_rows, buy_set_name = parse_listing_page(html, game, set_code, "buy")
        set_name = set_name or buy_set_name
        before = len(buy_rows)
        buy_rows = [r for r in buy_rows if r["setCode"].lower() == set_code.lower()]
        dropped = before - len(buy_rows)
        print(f"  -> parsed {len(buy_rows)} buy rows"
              + (f"  [dropped {dropped} unrelated/pickup cards]" if dropped else ""))

    return merge_rows(sell_rows, buy_rows, mode), set_name


def merge_rows(sell_rows, buy_rows, mode):
    by_number = {}

    for r in sell_rows:
        rec = CardRecord(
            game=r["game"], setCode=r["setCode"], cardId=r["cardId"],
            cardNumber=r["cardNumber"], name=r["name"], rarity=r["rarity"],
            stock=r["stock"], availability=r["availability"],
            imageUrl=r["imageUrl"], url=r["url"],
        )
        # first (non-discount) price is the sell price
        if r["prices"]:
            rec.sellPriceJpy = r["prices"][0]
        by_number[r["cardNumber"]] = rec

    for r in buy_rows:
        rec = by_number.get(r["cardNumber"])
        if rec is None:
            rec = CardRecord(
                game=r["game"], setCode=r["setCode"], cardId=r["cardId"],
                cardNumber=r["cardNumber"], name=r["name"], rarity=r["rarity"],
                imageUrl=r["imageUrl"], url=r["url"],
            )
            by_number[r["cardNumber"]] = rec

        prices = r["prices"]
        if r["boosted"] and len(prices) >= 2:
            # boosted listings show both the boosted and base buy price
            rec.buyPriceJpy = max(prices[:2])
            rec.buyPriceBaseJpy = min(prices[:2])
            rec.buyPriceBoosted = True
        elif prices:
            rec.buyPriceJpy = prices[0]

    records = list(by_number.values())
    for rec in records:
        if rec.sellPriceJpy is not None and rec.buyPriceJpy is not None:
            rec.spreadJpy = rec.sellPriceJpy - rec.buyPriceJpy
        if mode == "sell":
            rec.buyPriceJpy = rec.buyPriceBaseJpy = None
            rec.buyPriceBoosted = False
        if mode == "buy":
            rec.sellPriceJpy = None

    return records


def update_site_data(records, game: str, set_code: str, mode: str, set_name: Optional[str] = None,
                      site_dir: str = "docs", alias: Optional[str] = None):
    """
    Writes per-set JSON into <site_dir>/data/ and keeps data/manifest.json
    up to date, so the index.html viewer can list available game/set
    combos and load them on demand. Safe to call repeatedly; re-scraping a
    set just overwrites its entry.
    """
    data_dir = os.path.join(site_dir, "data")
    os.makedirs(data_dir, exist_ok=True)

    fname = f"{game}_{set_code}.json"
    fpath = os.path.join(data_dir, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in records], f, ensure_ascii=False, indent=2)

    manifest_path = os.path.join(data_dir, "manifest.json")
    manifest = []
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            try:
                manifest = json.load(f)
            except json.JSONDecodeError:
                manifest = []

    existing = next((m for m in manifest if m["game"] == game and m["set"] == set_code), None)
    manifest = [m for m in manifest if not (m["game"] == game and m["set"] == set_code)]

    aliases = list(existing.get("aliases", [])) if existing else []
    if alias and alias not in aliases:
        aliases.append(alias)

    manifest.append({
        "game": game,
        "set": set_code,
        "name": set_name or set_code,
        "aliases": aliases,
        "file": f"data/{fname}",
        "mode": mode,
        "count": len(records),
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
    })
    manifest.sort(key=lambda m: (m["game"], m["set"]))

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"Updated site data -> {fpath} (+ manifest.json, {len(manifest)} set(s) total)")


def _normalize_for_match(s: str) -> str:
    return re.sub(r"[\s/\-]", "", s or "").lower()


def find_sets_by_keyword(session: requests.Session, game: str, keyword: str, delay: float):
    """
    Searches yuyu-tei's own keyword search (works for Japanese card names,
    and often for the printed set code like "OSK/S133" too, since it's part
    of the visible card text) and reports which actual set code(s) the
    matching cards belong to — so you can go from the code printed on a
    card straight to the URL slug yuyu-tei uses internally.

    Filters out unrelated cards from "pickup"/"recommended" panels that
    yuyu-tei shows alongside real results, by requiring the keyword to
    actually appear in each card's own number or name.
    """
    url = f"{BASE_URL}/sell/{game}/s/search"
    resp = session.get(url, params={"search_word": keyword, "kizu": "0"}, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    time.sleep(delay)
    rows, _ = parse_listing_page(resp.text, game, "search", "sell")

    kw_norm = _normalize_for_match(keyword)
    tally = {}  # setCode -> {"count": n, "example": name}
    for r in rows:
        haystack = _normalize_for_match(r["cardNumber"]) + _normalize_for_match(r["name"])
        if kw_norm not in haystack:
            continue  # not an actual match — likely a pickup/recommended card
        sc = r["setCode"]
        if sc not in tally:
            tally[sc] = {"count": 0, "example": r["name"]}
        tally[sc]["count"] += 1

    return tally


def scrape_by_card_code(session: requests.Session, game: str, card_code: str, mode: str,
                         delay: float, debug: bool = False):
    """
    Scrapes directly using the code printed on the card (e.g. "OSK/S133"),
    via yuyu-tei's own keyword search — no need to first translate it into
    yuyu-tei's internal URL slug. Filters out anything that doesn't
    actually contain the searched code (pickup/recommended cards).
    """
    kw_norm = _normalize_for_match(card_code)
    sell_rows, buy_rows = [], []

    if mode in ("sell", "both"):
        url = f"{BASE_URL}/sell/{game}/s/search"
        print(f'Searching sell listings for "{card_code}": {url}')
        resp = session.get(url, params={"search_word": card_code, "kizu": "0"}, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        time.sleep(delay)
        if debug:
            with open("debug_page.html", "w", encoding="utf-8") as f:
                f.write(resp.text)
            print("[debug] saved raw HTML -> debug_page.html")
        rows, _ = parse_listing_page(resp.text, game, "search", "sell")
        before = len(rows)
        sell_rows = [r for r in rows
                     if kw_norm in _normalize_for_match(r["cardNumber"]) + _normalize_for_match(r["name"])]
        print(f"  -> matched {len(sell_rows)} sell rows (dropped {before - len(sell_rows)} unrelated)")

    if mode in ("buy", "both"):
        url = f"{BASE_URL}/buy/{game}/s/search"
        print(f'Searching buylist for "{card_code}": {url}')
        resp = session.get(url, params={"search_word": card_code, "kizu": "0"}, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        time.sleep(delay)
        rows, _ = parse_listing_page(resp.text, game, "search", "buy")
        before = len(rows)
        buy_rows = [r for r in rows
                    if kw_norm in _normalize_for_match(r["cardNumber"]) + _normalize_for_match(r["name"])]
        print(f"  -> matched {len(buy_rows)} buy rows (dropped {before - len(buy_rows)} unrelated)")

    records = merge_rows(sell_rows, buy_rows, mode)

    # A broad query like "OSK" can genuinely match cards across several
    # real yuyu-tei sets (e.g. osk, osk2.0, osk3.0) at once — group by each
    # card's own set rather than mislabeling everything with just the
    # first match's set.
    by_set = {}
    for rec in records:
        by_set.setdefault(rec.setCode, []).append(rec)

    groups = []
    for set_code, recs in by_set.items():
        set_name = lookup_set_name(session, game, set_code, delay) if set_code else None
        groups.append((set_code, set_name, recs))

    return records, groups


def write_output(records, out_path: str):
    if not records:
        print("No records scraped — nothing written.")
        return

    if out_path.lower().endswith(".json"):
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in records], f, ensure_ascii=False, indent=2)
    else:
        fieldnames = list(asdict(records[0]).keys())
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in records:
                writer.writerow(asdict(r))

    print(f"Wrote {len(records)} records -> {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Scrape yuyu-tei.jp sell/buylist prices for a set.")
    parser.add_argument("--game", default=DEFAULT_GAME, help="Game section code, e.g. ws, poc, ygo, opc, dm, ua, vg, digi, bs")
    parser.add_argument("--set", dest="sets", action="append",
                         help="Set code from the set's URL (yuyu-tei.jp/sell/<game>/s/<setCode>). Repeatable.")
    parser.add_argument("--mode", choices=["sell", "buy", "both"], default=DEFAULT_MODE)
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output file (.csv or .json)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="Seconds to sleep between requests")
    parser.add_argument("--debug", action="store_true", help="Dump raw HTML of each fetched page to debug_page.html")
    parser.add_argument("--site", action="store_true",
                         help="Also write per-set JSON + manifest.json into docs/data/ for the index.html viewer")
    parser.add_argument("--site-dir", default="docs", help="Folder the GitHub Pages site lives in (default: docs)")
    parser.add_argument("--lookup", metavar="SET_CODE",
                         help="Just check what a set code's real name is (1 quick request), don't scrape cards")
    parser.add_argument("--find-set", metavar="KEYWORD",
                         help='Find yuyu-tei\'s internal set code from the code printed on a card, '
                              'e.g. --find-set "OSK/S133" (or a Japanese card name)')
    parser.add_argument("--card-code", metavar="CODE",
                         help='Scrape directly using the code printed on the card, e.g. '
                              '--card-code "OSK/S133" — skips needing the internal slug at all')
    args = parser.parse_args()

    session = requests.Session()

    if args.card_code:
        records, groups = scrape_by_card_code(session, args.game, args.card_code, args.mode, args.delay, args.debug)
        if not records:
            print(f'No cards found for "{args.card_code}".')
            return
        print(f'\n{len(records)} card(s) found across {len(groups)} set(s):')
        for set_code, set_name, recs in groups:
            print(f'  {set_code:15s} ({len(recs):3d} cards)  {set_name or ""}')
            if args.site:
                update_site_data(recs, args.game, set_code, args.mode, set_name, args.site_dir, alias=args.card_code)
        write_output(records, args.out)
        return

    if args.find_set:
        print(f'Searching yuyu-tei for "{args.find_set}"...')
        tally = find_sets_by_keyword(session, args.game, args.find_set, args.delay)
        if not tally:
            print("No matches. If you used the printed set code, try just part of it, "
                  "or search by a Japanese card name from that set instead.")
            return
        print(f"Found cards from {len(tally)} set(s):")
        for set_code, info in sorted(tally.items(), key=lambda kv: -kv[1]["count"]):
            name = lookup_set_name(session, args.game, set_code, args.delay) or "?"
            print(f'  {set_code:15s} ({info["count"]:3d} matching cards)  -> {name}')
        print("\nRun this for whichever one is right:")
        best = max(tally.items(), key=lambda kv: kv[1]["count"])[0]
        print(f"  python yuyutei_scraper.py --game {args.game} --set {best} --mode both --site")
        return

    if args.lookup:
        name = lookup_set_name(session, args.game, args.lookup, args.delay)
        if name:
            print(f'\n"{args.lookup}" is: {name}\n')
            print(f"If that's the right set, run:")
            print(f"  python yuyutei_scraper.py --game {args.game} --set {args.lookup} --mode both --site")
        else:
            print(f'Could not find a set name for "{args.lookup}" — double check the code.')
        return

    if not args.sets:
        args.sets = DEFAULT_SETS

    all_records = []

    for set_code in args.sets:
        try:
            records, set_name = scrape_set(session, args.game, set_code, args.mode, args.delay, args.debug)
            all_records.extend(records)
            if args.site:
                update_site_data(records, args.game, set_code, args.mode, set_name, args.site_dir)
        except requests.HTTPError as e:
            print(f"[error] HTTP error scraping {set_code}: {e}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"[error] Network error scraping {set_code}: {e}", file=sys.stderr)

    write_output(all_records, args.out)


if __name__ == "__main__":
    main()
