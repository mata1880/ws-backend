"""
Wraps the existing, already-working scraper modules (unmodified) and feeds
their output into the database instead of docs/data/*.json. This is the
only place that touches yuyutei_scraper / wstcg_scraper directly.
"""
import re
from dataclasses import asdict

import requests
from sqlalchemy.orm import Session

from . import models
from .scraping import yuyutei_scraper as yuyutei
from .scraping import wstcg_scraper as wstcg


def _get_or_create_card(db: Session, card_number: str, game: str, set_code: str) -> models.Card:
    card = db.query(models.Card).filter(models.Card.card_number == card_number).first()
    if card is None:
        card = models.Card(card_number=card_number, game=game, set_code=set_code, name="")
        db.add(card)
        db.flush()  # get card.id without committing yet
    return card


BULK_RARITIES_SKIP_PRICE = {"C", "U", "R", "CR", "CX"}


def run_price_scrape(db: Session, game: str, card_code: str, mode: str, delay: float = 1.5, skip_bulk_rarities: bool = False):
    """
    Runs the exact same logic as `yuyutei_scraper.py --card-code`, but
    writes results into cards + price_snapshots instead of docs/data/.
    Every call adds NEW price_snapshot rows — nothing is overwritten, so
    price history and collection-value-over-time just fall out of the data.

    skip_bulk_rarities: when scraping a whole title from the Prices page,
    C/U/R/CR/CX cards still get added/updated as Card rows (so they show
    up in Browse) but don't get a price fetched — those are cheap bulk
    rarities not worth the scrape time across a whole title. A targeted
    price check on a specific wishlist/collection/binder never sets this,
    so those rarities still get real prices when you actually own one.
    """
    session = requests.Session()
    records, groups = yuyutei.scrape_by_card_code(session, game, card_code, mode, delay)

    cards_seen = 0
    snapshots_added = 0
    sets_touched = []

    for set_code, set_name, recs in groups:
        sets_touched.append(f"{set_code} ({set_name or '?'}) — {len(recs)} cards")

    for rec in records:
        card = _get_or_create_card(db, rec.cardNumber, rec.game, rec.setCode)
        # keep name/rarity/image current — cheap, and yuyu-tei sometimes has
        # these when the catalog scrape hasn't been run for this card yet
        if rec.name:
            card.name = rec.name
        if rec.rarity:
            card.rarity = rec.rarity
        if rec.imageUrl and not card.image_url:
            card.image_url = rec.imageUrl
        cards_seen += 1

        if skip_bulk_rarities and (rec.rarity or "").strip().upper() in BULK_RARITIES_SKIP_PRICE:
            continue

        snap = models.PriceSnapshot(
            card_id=card.id,
            sell_price_jpy=rec.sellPriceJpy,
            buy_price_jpy=rec.buyPriceJpy,
            buy_price_boosted=bool(rec.buyPriceBoosted),
            stock=rec.stock,
            availability=rec.availability,
        )
        db.add(snap)
        snapshots_added += 1

    db.commit()
    return {"cards_seen": cards_seen, "price_snapshots_added": snapshots_added, "sets": sets_touched}


def _base_card_number(cn: str) -> str:
    # Mirrors the frontend's baseCardNumber(): strip a trailing rarity
    # suffix like "SSP"/"S"/"+" so a yuyu-tei card_number ("OSK/S121-002SSP")
    # can match the catalog's plain number ("OSK/S121-002").
    return re.sub(r"[A-Z+]+$", "", cn or "")


MAX_PRICE_CHECK_CARDS = 50  # a single request with no progress bar and no partial-recovery if interrupted — kept well short of the 100-min platform timeout on purpose


def run_price_check(db: Session, cards, delay: float = 1.2):
    """
    Re-scrapes current prices for a specific set of cards (e.g. everything
    in one collection/wishlist/binder) — one exact-card-number search per
    card, not a broad set-wide scrape. More requests than a set-level
    scrape, but only touches the cards you actually asked about.

    If there are more than MAX_PRICE_CHECK_CARDS, cards that have never
    been price-checked (or were checked longest ago) go first — so
    clicking the button again on a big list naturally works through the
    rest over a few clicks, rather than re-checking the same first 50
    every time and never reaching the tail end.
    """
    unique = []
    seen = set()
    for c in cards:
        if c.card_number and c.card_number not in seen:
            seen.add(c.card_number)
            unique.append(c)

    latest_by_card_id = {}
    if unique:
        card_ids = [c.id for c in unique]
        for snap in (
            db.query(models.PriceSnapshot)
            .filter(models.PriceSnapshot.card_id.in_(card_ids))
            .order_by(models.PriceSnapshot.scraped_at.desc())
            .all()
        ):
            latest_by_card_id.setdefault(snap.card_id, snap.scraped_at)

    # never-checked cards (no entry at all) sort first, via datetime.min;
    # otherwise oldest-checked first.
    from datetime import datetime as _dt
    unique.sort(key=lambda c: latest_by_card_id.get(c.id, _dt.min))

    codes = [c.card_number for c in unique]
    truncated = len(codes) > MAX_PRICE_CHECK_CARDS
    codes = codes[:MAX_PRICE_CHECK_CARDS]

    results = []
    total_snapshots = 0
    for code in codes:
        r = run_price_scrape(db, "ws", code, "both", delay)
        results.append({"prefix": code, **r})
        total_snapshots += r["price_snapshots_added"]

    return {
        "prefixes_checked": codes,
        "total_price_snapshots_added": total_snapshots,
        "truncated": truncated,
    }


def run_catalog_scrape(db: Session, query: str, delay: float = 1.0):
    """
    Runs the exact same logic as `wstcg_scraper.py --query`, and merges the
    official card details (text, stats, real expansion name) onto whichever
    Card rows match — by exact card_number first, falling back to the base
    number (parallels/foils share the same text as their base print).
    """
    session = requests.Session()
    catalog_cards, total = wstcg.fetch_all(session, query, delay)

    # Build a lookup of existing cards by exact number and by base number,
    # so we're not doing a linear scan per catalog card.
    existing = db.query(models.Card).all()
    by_exact = {c.card_number: c for c in existing}
    by_base = {}
    for c in existing:
        by_base.setdefault(_base_card_number(c.card_number), c)

    cards_seen = 0
    for cc in catalog_cards:
        if not cc.cardNumber:
            continue
        card = by_exact.get(cc.cardNumber) or by_base.get(_base_card_number(cc.cardNumber))
        if card is None:
            # No price data for this one yet — still worth storing, so it
            # shows up once a price scrape catches up to it later.
            card = models.Card(
                card_number=cc.cardNumber, game="ws",
                set_code=(cc.titleNumber or "").lower(), name=cc.name or "",
            )
            db.add(card)
            db.flush()
            by_exact[card.card_number] = card

        card.name = cc.name or card.name
        card.rarity = cc.rarity or card.rarity
        if cc.imageUrl:
            card.image_url = cc.imageUrl  # official image is higher quality than yuyu-tei's thumbnail
        card.expansion_name = cc.expansionName or card.expansion_name
        card.title_number = cc.titleNumber or card.title_number
        card.color = cc.color
        card.level = cc.level
        card.cost = cc.cost
        card.power = cc.power
        card.soul = cc.soul
        card.trigger = cc.trigger
        card.traits = ", ".join(cc.traits) if cc.traits else None
        card.text = cc.text
        card.flavor = cc.flavor
        cards_seen += 1

    db.commit()
    return {"cards_seen": cards_seen, "total_reported_by_site": total}
