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

    # Batch-fetch every card this scrape touches in ONE query instead of
    # one query per record — for a big title (hundreds of cards) this used
    # to mean hundreds of separate round-trips to the database before any
    # actual price work even happened, which dominated runtime far more
    # than the yuyu-tei requests themselves did.
    card_numbers = list({rec.cardNumber for rec in records if rec.cardNumber})
    cards_by_number = {}
    if card_numbers:
        for c in db.query(models.Card).filter(models.Card.card_number.in_(card_numbers)).all():
            cards_by_number[c.card_number] = c
    for rec in records:
        if rec.cardNumber and rec.cardNumber not in cards_by_number:
            new_card = models.Card(card_number=rec.cardNumber, game=rec.game, set_code=rec.setCode, name="")
            db.add(new_card)
            cards_by_number[rec.cardNumber] = new_card
    db.flush()  # assigns .id to every new card in one batch, not one per record

    new_snapshots = []
    for rec in records:
        card = cards_by_number[rec.cardNumber]
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

        new_snapshots.append(models.PriceSnapshot(
            card_id=card.id,
            sell_price_jpy=rec.sellPriceJpy,
            buy_price_jpy=rec.buyPriceJpy,
            buy_price_boosted=bool(rec.buyPriceBoosted),
            stock=rec.stock,
            availability=rec.availability,
        ))
        snapshots_added += 1

    # A real batch insert (one round-trip for all of them), not `db.add()`
    # in a loop — every scrape creates a fresh snapshot per card by design,
    # so for a big title this is hundreds of rows every single time, and
    # `db.add()` one at a time was still issuing one INSERT round-trip per
    # row even inside a single commit. This was the other half of the
    # slowdown the batched card-lookup fix didn't cover.
    if new_snapshots:
        db.bulk_save_objects(new_snapshots)

    db.commit()
    return {"cards_seen": cards_seen, "price_snapshots_added": snapshots_added, "sets": sets_touched}


def _base_card_number(cn: str) -> str:
    # Mirrors the frontend's baseCardNumber(): strip a trailing rarity
    # suffix like "SSP"/"S"/"+" so a yuyu-tei card_number ("OSK/S121-002SSP")
    # can match the catalog's plain number ("OSK/S121-002").
    return re.sub(r"[A-Z+]+$", "", cn or "")


MAX_PRICE_CHECK_CARDS = 50  # a single request with no progress bar and no partial-recovery if interrupted — kept well short of the 100-min platform timeout on purpose
MAX_PRICE_UPDATE_TITLES = 25  # title-level scrapes cover many cards each, so this can stay small — only matters when only_titles isn't given (the frontend's per-title loop bypasses this cap entirely, calling once per title regardless of count)


def _title_prefix(card_number: str):
    # Mirrors the frontend's W.titlePrefix(): the leading 2-4 letters
    # before the first "/" — e.g. "OSK" from "OSK/S133-001SSP". Matches
    # what --card-code / the Browse title filter already treat as one title.
    before = (card_number or "").split("/")[0]
    m = re.match(r"^[A-Za-z]{2,4}", before)
    return m.group(0).upper() if m else (before or None)


def _set_code_prefix(card_number: str):
    # The SPECIFIC expansion code — e.g. "SAO/S71" from "SAO/S71-001R" —
    # not just the broad title ("SAO"). A franchise like SAO can span many
    # expansions; if you only own cards from S71, there's no reason a
    # price-update search should also crawl through every other SAO
    # release. Everything before the first "-" is exactly this, since
    # yuyu-tei card numbers are always "TITLE/SETCODE-cardnum".
    return (card_number or "").split("-", 1)[0] or None


def run_price_check(db: Session, cards, delay: float = 1.2):
    """
    Fetches a price ONLY for cards in this list that don't have any price
    data at all yet — the fast path, since it skips everything already
    priced instead of re-doing work. Good for "I just added some new
    cards, get them a starting price" without waiting on cards you
    already checked. For actually refreshing prices you already have,
    use run_price_update instead.
    """
    unique = []
    seen = set()
    for c in cards:
        if c.card_number and c.card_number not in seen:
            seen.add(c.card_number)
            unique.append(c)

    if not unique:
        return {"prefixes_checked": [], "total_price_snapshots_added": 0, "truncated": False}

    card_ids = [c.id for c in unique]
    has_price = {
        row[0] for row in
        db.query(models.PriceSnapshot.card_id).filter(models.PriceSnapshot.card_id.in_(card_ids)).distinct()
    }
    to_check = [c for c in unique if c.id not in has_price]

    truncated = len(to_check) > MAX_PRICE_CHECK_CARDS
    to_check = to_check[:MAX_PRICE_CHECK_CARDS]

    total_snapshots = 0
    for c in to_check:
        r = run_price_scrape(db, "ws", c.card_number, "both", delay)
        total_snapshots += r["price_snapshots_added"]

    return {
        "prefixes_checked": [c.card_number for c in to_check],
        "total_price_snapshots_added": total_snapshots,
        "truncated": truncated,
    }


def run_price_update(db: Session, cards, delay: float = 1.2, only_titles=None):
    """
    Re-checks prices for this list of cards by SPECIFIC expansion, not one
    card at a time and not by broad franchise either: groups cards by the
    exact set code (e.g. "SAO/S71" from "SAO/S71-001R", not just "SAO")
    and runs one scrape per distinct expansion. If you only own cards from
    one SAO release, this never touches any of SAO's other expansions —
    keeping each individual search small even for a franchise with many
    volumes, which also makes any single request far less likely to be
    slow enough to time out.

    only_titles: if given, restricts to exactly these set codes instead of
    auto-computing (and capping at MAX_PRICE_UPDATE_TITLES) the full set
    — used by the frontend to process one at a time for a real,
    incremental progress counter instead of one long blocking call.
    """
    unique = []
    seen = set()
    for c in cards:
        if c.card_number and c.card_number not in seen:
            seen.add(c.card_number)
            unique.append(c)

    if not unique:
        return {"checked": 0, "changed": [], "total_price_snapshots_added": 0, "truncated": False}

    def latest_prices(card_id):
        snap = (
            db.query(models.PriceSnapshot)
            .filter(models.PriceSnapshot.card_id == card_id)
            .order_by(models.PriceSnapshot.scraped_at.desc())
            .first()
        )
        return (snap.sell_price_jpy, snap.buy_price_jpy) if snap else (None, None)

    before_by_card = {c.id: latest_prices(c.id) for c in unique}

    if only_titles is not None:
        titles = list(only_titles)
        truncated = False
    else:
        titles = []
        for c in unique:
            t = _set_code_prefix(c.card_number)
            if t and t not in titles:
                titles.append(t)
        truncated = len(titles) > MAX_PRICE_UPDATE_TITLES
        titles = titles[:MAX_PRICE_UPDATE_TITLES]
    titles_set = set(titles)

    total_snapshots = 0
    for title in titles:
        r = run_price_scrape(db, "ws", title, "both", delay)
        total_snapshots += r["price_snapshots_added"]

    changed = []
    checked = 0
    for c in unique:
        if _set_code_prefix(c.card_number) not in titles_set:
            continue  # this card's title wasn't covered this round (truncated) — try again next click
        checked += 1
        before_sell, before_buy = before_by_card[c.id]
        after_sell, after_buy = latest_prices(c.id)
        if after_sell != before_sell or after_buy != before_buy:
            changed.append({
                "card_number": c.card_number,
                "name": c.name,
                "old_sell_price_jpy": before_sell,
                "new_sell_price_jpy": after_sell,
                "old_buy_price_jpy": before_buy,
                "new_buy_price_jpy": after_buy,
            })

    return {
        "checked": checked,
        "changed": changed,
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
        # NOT overwritten from the catalog: yuyu-tei's own per-card data
        # distinguishes sub-variants like "SR1"/"SR2"/"SR3", but ws-tcg.com's
        # official catalog just says generic "SR" — overwriting here would
        # silently collapse that real distinction every time this runs.
        # Only fill rarity in if we don't have one at all yet.
        card.rarity = card.rarity or cc.rarity
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


def run_gcg_catalog_scrape(db: Session, query: str, delay: float = 0.6):
    """
    Gundam version of "Get card info": reads the official Japanese card list
    (gundam-gcg.com) for the set(s) matching `query` (e.g. "GD01", "ST01").
    One request at a time with a pause between them. Cards that already have
    their text and image are skipped, so only the first run of a set is slow.
    Each card is stored under its site id (GD01-001, or GD01-001_p1 for a
    parallel), since Gundam parallels share their printed number.
    """
    import re as _re
    import time as _time
    from .scraping import gcg_scraper as gcg

    session = requests.Session()
    sets = gcg.resolve_sets(gcg.fetch_sets(session), query)
    if not sets:
        raise ValueError(f"No Gundam set matches '{query}'. Try a code like GD01, ST01 or EB01.")

    existing = {c.card_number: c for c in db.query(models.Card).filter(models.Card.game == "gcg").all()}
    cards_seen = skipped = total = 0
    for package_id, set_name in sets:
        m = _re.search(r"\[([A-Za-z0-9]+)\]", set_name)
        set_code = (m.group(1) if m else package_id).lower()
        _time.sleep(delay)
        ids = gcg.fetch_card_ids(session, package_id)
        total += len(ids)
        for detail_id in ids:
            card = existing.get(detail_id)
            if card is not None and card.text and card.image_url:
                skipped += 1
                continue
            _time.sleep(delay)
            d = gcg.fetch_detail(session, detail_id)
            if card is None:
                card = models.Card(card_number=detail_id, game="gcg", language="ja",
                                   set_code=set_code, name=d.name or "")
                db.add(card)
                db.flush()
                existing[detail_id] = card
            card.name = d.name or card.name
            card.rarity = card.rarity or d.rarity
            card.image_url = d.image_url or card.image_url
            card.expansion_name = d.set_name or set_name
            card.title_number = set_code.upper()
            card.color, card.level, card.cost = d.color, d.level, d.cost
            card.power, card.soul = d.ap, d.hp          # AP / HP (Weiss power/soul slots)
            card.trigger = d.card_type                   # UNIT / PILOT / COMMAND / BASE ...
            card.traits = ", ".join(d.traits) if d.traits else None
            card.text = d.text
            extras = [f"地形: {d.terrain}" if d.terrain else None,
                      f"リンク: {d.link}" if d.link else None,
                      f"出典: {d.source_title}" if d.source_title else None]
            card.flavor = " · ".join(x for x in extras if x) or None
            cards_seen += 1
        db.commit()   # one commit per set, so a failure later doesn't lose finished sets

    return {"cards_seen": cards_seen, "skipped_already_complete": skipped,
            "total_reported_by_site": total, "sets": [n for _, n in sets]}
