from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, schemas, utils
from ..database import get_db

router = APIRouter(prefix="/cards", tags=["cards"])


@router.get("", response_model=List[schemas.CardWithPrice])
def list_cards(
    search: Optional[str] = Query(None, description="Matches name or card_number, case-insensitive"),
    set_code: Optional[str] = None,
    rarity: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(models.Card)
    if search:
        like = f"%{search}%"
        q = q.filter((models.Card.name.ilike(like)) | (models.Card.card_number.ilike(like)))
    if set_code:
        q = q.filter(models.Card.set_code == set_code)
    if rarity:
        q = q.filter(models.Card.rarity == rarity)
    cards = q.all()
    card_ids = [c.id for c in cards]

    # One query for wishlist membership across all cards, instead of one
    # query per card — a card can only be on one wishlist at a time, so
    # this is a simple id -> wishlist_id map.
    wishlist_by_card = {
        wi.card_id: wi.wishlist_id
        for wi in db.query(models.WishlistItem).all()
    }

    # Latest price per card in two aggregate queries total, not one query
    # per card. Step 1: the latest timestamp per card (cheap aggregation,
    # stays fast even as price history grows). Step 2: join back to the
    # actual snapshot rows at those timestamps.
    latest_by_card = {}
    owned_by_card = {}
    if card_ids:
        latest_times = (
            db.query(models.PriceSnapshot.card_id, func.max(models.PriceSnapshot.scraped_at).label("max_time"))
            .filter(models.PriceSnapshot.card_id.in_(card_ids))
            .group_by(models.PriceSnapshot.card_id)
            .subquery()
        )
        latest_snapshots = (
            db.query(models.PriceSnapshot)
            .join(
                latest_times,
                (models.PriceSnapshot.card_id == latest_times.c.card_id)
                & (models.PriceSnapshot.scraped_at == latest_times.c.max_time),
            )
            .all()
        )
        latest_by_card = {s.card_id: s for s in latest_snapshots}

        owned_by_card = dict(
            db.query(models.Copy.card_id, func.count(models.Copy.id))
            .filter(models.Copy.card_id.in_(card_ids))
            .group_by(models.Copy.card_id)
            .all()
        )

    out = []
    for c in cards:
        latest = latest_by_card.get(c.id)
        out.append(schemas.CardWithPrice(
            **schemas.CardOut.model_validate(c).model_dump(),
            sell_price_jpy=latest.sell_price_jpy if latest else None,
            buy_price_jpy=latest.buy_price_jpy if latest else None,
            price_scraped_at=latest.scraped_at if latest else None,
            owned_copies=owned_by_card.get(c.id, 0),
            wishlist_id=wishlist_by_card.get(c.id),
            availability=latest.availability if latest else None,
        ))
    return out


@router.get("/{card_id}", response_model=schemas.CardWithPrice)
def get_card(card_id: int, db: Session = Depends(get_db)):
    c = db.query(models.Card).get(card_id)
    if not c:
        raise HTTPException(404, "Card not found")
    return utils.card_with_price(db, c)


@router.get("/{card_id}/price-history", response_model=List[schemas.PriceSnapshotOut])
def price_history(card_id: int, db: Session = Depends(get_db)):
    c = db.query(models.Card).get(card_id)
    if not c:
        raise HTTPException(404, "Card not found")
    return (
        db.query(models.PriceSnapshot)
        .filter(models.PriceSnapshot.card_id == card_id)
        .order_by(models.PriceSnapshot.scraped_at.asc())
        .all()
    )
