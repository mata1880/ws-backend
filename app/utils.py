from sqlalchemy.orm import Session
from sqlalchemy import func

from . import models, schemas


def _trend(latest_val, prev_val):
    if latest_val is None or prev_val is None:
        return None
    if latest_val > prev_val:
        return "up"
    if latest_val < prev_val:
        return "down"
    return "same"


def _batch_price_trends(db: Session, card_ids: list):
    """
    For each card, returns its latest snapshot plus an up/down/same trend
    for sell and buy vs. the previous snapshot. Fetches every matching
    snapshot in ONE query — ordered so each card's own rows land together,
    most recent first — rather than one query per card. {card_id: {...}}.
    """
    if not card_ids:
        return {}
    rows = (
        db.query(models.PriceSnapshot)
        .filter(models.PriceSnapshot.card_id.in_(card_ids))
        .order_by(models.PriceSnapshot.card_id, models.PriceSnapshot.scraped_at.desc())
        .all()
    )
    by_card = {}
    for r in rows:
        lst = by_card.setdefault(r.card_id, [])
        if len(lst) < 2:  # only need the latest + the one before it
            lst.append(r)

    result = {}
    for card_id, snaps in by_card.items():
        latest = snaps[0]
        prev = snaps[1] if len(snaps) > 1 else None
        result[card_id] = {
            "latest": latest,
            "sell_trend": _trend(latest.sell_price_jpy, prev.sell_price_jpy if prev else None),
            "buy_trend": _trend(latest.buy_price_jpy, prev.buy_price_jpy if prev else None),
        }
    return result


def cards_with_price_batch(db: Session, card_list) -> dict:
    """
    Builds CardWithPrice for a whole LIST of Card rows in a handful of
    batch queries total, instead of one query per card. Use this anywhere
    more than one card needs prices attached (Browse, a whole collection,
    a whole wishlist) — calling card_with_price() in a per-card loop is
    exactly the N+1 pattern that made /cards slow before it was fixed;
    this is the same fix, reused. Returns {card_id: CardWithPrice}.
    """
    card_ids = [c.id for c in card_list]
    wishlist_by_card = {}
    owned_by_card = {}
    in_binder_ids = set()
    if card_ids:
        wishlist_by_card = {
            wi.card_id: wi.wishlist_id
            for wi in db.query(models.WishlistItem).filter(models.WishlistItem.card_id.in_(card_ids)).all()
        }
        owned_by_card = dict(
            db.query(models.Copy.card_id, func.count(models.Copy.id))
            .filter(models.Copy.card_id.in_(card_ids))
            .group_by(models.Copy.card_id)
            .all()
        )
        in_binder_ids = {
            row.card_id for row in
            db.query(models.BinderSlot.card_id).filter(models.BinderSlot.card_id.in_(card_ids)).distinct()
        }
    trends = _batch_price_trends(db, card_ids)

    out = {}
    for c in card_list:
        info = trends.get(c.id)
        latest = info["latest"] if info else None
        out[c.id] = schemas.CardWithPrice(
            **schemas.CardOut.model_validate(c).model_dump(),
            sell_price_jpy=latest.sell_price_jpy if latest else None,
            buy_price_jpy=latest.buy_price_jpy if latest else None,
            price_scraped_at=latest.scraped_at if latest else None,
            owned_copies=owned_by_card.get(c.id, 0),
            wishlist_id=wishlist_by_card.get(c.id),
            availability=latest.availability if latest else None,
            sell_trend=info["sell_trend"] if info else None,
            buy_trend=info["buy_trend"] if info else None,
            in_binder=c.id in in_binder_ids,
        )
    return out


def card_with_price(db: Session, card: models.Card) -> schemas.CardWithPrice:
    """
    Single-card version of the above — fine to use for genuine one-off
    lookups (e.g. GET /cards/{id}), but never in a loop over a list; use
    cards_with_price_batch() for that instead.
    """
    return cards_with_price_batch(db, [card])[card.id]
