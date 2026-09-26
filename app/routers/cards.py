from typing import Optional, List

from urllib.parse import urlparse

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, Response
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


# Official card-image hosts whose images may be relayed. Kept to an explicit
# list so this can't be used to fetch arbitrary sites.
_IMAGE_HOSTS = {"www.gundam-gcg.com": "https://www.gundam-gcg.com/jp/cards/",
                "gundam-gcg.com": "https://www.gundam-gcg.com/jp/cards/"}


@router.get("/image")
def relay_card_image(url: str):
    """
    Fetches an official card image and passes it on, for sites that refuse to
    serve images to pages on other domains (hotlink protection). Only hosts in
    _IMAGE_HOSTS are allowed. Registered BEFORE "/{card_id}" on purpose: FastAPI
    matches in order, and "image" would otherwise be taken as a card id.
    """
    parsed = urlparse(url)
    referer = _IMAGE_HOSTS.get(parsed.hostname or "")
    if parsed.scheme != "https" or referer is None:
        raise HTTPException(400, "Image host not allowed.")
    try:
        r = requests.get(url, headers={"Referer": referer, "User-Agent": "Mozilla/5.0"}, timeout=20)
    except requests.RequestException as e:
        raise HTTPException(502, f"Couldn't fetch image: {e}")
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
        raise HTTPException(502, f"Image source answered {r.status_code}.")
    return Response(content=r.content, media_type=r.headers["content-type"],
                    headers={"Cache-Control": "public, max-age=604800"})   # browsers keep it a week


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
