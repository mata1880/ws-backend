from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, schemas
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

    # One query for wishlist membership across all cards, instead of one
    # query per card — a card can only be on one wishlist at a time, so
    # this is a simple id -> wishlist_id map.
    wishlist_by_card = {
        wi.card_id: wi.wishlist_id
        for wi in db.query(models.WishlistItem).all()
    }

    out = []
    for c in cards:
        latest = (
            db.query(models.PriceSnapshot)
            .filter(models.PriceSnapshot.card_id == c.id)
            .order_by(models.PriceSnapshot.scraped_at.desc())
            .first()
        )
        owned = db.query(func.count(models.Copy.id)).filter(models.Copy.card_id == c.id).scalar()
        out.append(schemas.CardWithPrice(
            **schemas.CardOut.model_validate(c).model_dump(),
            sell_price_jpy=latest.sell_price_jpy if latest else None,
            buy_price_jpy=latest.buy_price_jpy if latest else None,
            price_scraped_at=latest.scraped_at if latest else None,
            owned_copies=owned or 0,
            wishlist_id=wishlist_by_card.get(c.id),
        ))
    return out


@router.get("/{card_id}", response_model=schemas.CardWithPrice)
def get_card(card_id: int, db: Session = Depends(get_db)):
    c = db.query(models.Card).get(card_id)
    if not c:
        raise HTTPException(404, "Card not found")
    latest = (
        db.query(models.PriceSnapshot)
        .filter(models.PriceSnapshot.card_id == c.id)
        .order_by(models.PriceSnapshot.scraped_at.desc())
        .first()
    )
    owned = db.query(func.count(models.Copy.id)).filter(models.Copy.card_id == c.id).scalar()
    wi = db.query(models.WishlistItem).filter(models.WishlistItem.card_id == c.id).first()
    return schemas.CardWithPrice(
        **schemas.CardOut.model_validate(c).model_dump(),
        sell_price_jpy=latest.sell_price_jpy if latest else None,
        buy_price_jpy=latest.buy_price_jpy if latest else None,
        price_scraped_at=latest.scraped_at if latest else None,
        owned_copies=owned or 0,
        wishlist_id=wi.wishlist_id if wi else None,
    )


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
