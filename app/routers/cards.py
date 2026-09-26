from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models, schemas, utils
from ..database import get_db

router = APIRouter(prefix="/cards", tags=["cards"])


@router.get("", response_model=List[schemas.CardWithPrice])
def list_cards(
    search: Optional[str] = Query(None, description="Matches name or card_number, case-insensitive"),
    set_code: Optional[str] = None,
    rarity: Optional[str] = None,
    game: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(models.Card)
    if game:
        q = q.filter(models.Card.game == game)
    if search:
        like = f"%{search}%"
        q = q.filter((models.Card.name.ilike(like)) | (models.Card.card_number.ilike(like)))
    if set_code:
        q = q.filter(models.Card.set_code == set_code)
    if rarity:
        q = q.filter(models.Card.rarity == rarity)
    cards = q.all()
    by_id = utils.cards_with_price_batch(db, cards)
    return [by_id[c.id] for c in cards]


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
