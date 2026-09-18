from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas, utils, scrape_bridge
from ..database import get_db

router = APIRouter(prefix="/collections", tags=["collections"])


@router.get("", response_model=List[schemas.CollectionOut])
def list_collections(db: Session = Depends(get_db)):
    return db.query(models.Collection).order_by(models.Collection.name).all()


@router.post("", response_model=schemas.CollectionOut, status_code=201)
def create_collection(body: schemas.CollectionCreate, db: Session = Depends(get_db)):
    if db.query(models.Collection).filter(models.Collection.name == body.name).first():
        raise HTTPException(409, "A collection with that name already exists")
    c = models.Collection(name=body.name)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@router.patch("/{collection_id}", response_model=schemas.CollectionOut)
def rename_collection(collection_id: int, body: schemas.CollectionRename, db: Session = Depends(get_db)):
    c = db.query(models.Collection).get(collection_id)
    if not c:
        raise HTTPException(404, "Collection not found")
    c.name = body.name
    db.commit()
    db.refresh(c)
    return c


@router.delete("/{collection_id}", status_code=204)
def delete_collection(collection_id: int, db: Session = Depends(get_db)):
    c = db.query(models.Collection).get(collection_id)
    if not c:
        raise HTTPException(404, "Collection not found")
    # Copies aren't deleted — they just become unfiled (collection_id=None),
    # which is also exactly the "greyed out in binder" trigger. Nothing you
    # own silently disappears just because its collection got deleted.
    db.query(models.Copy).filter(models.Copy.collection_id == collection_id).update({"collection_id": None})
    db.delete(c)
    db.commit()


@router.get("/{collection_id}/copies", response_model=List[schemas.CopyWithCard])
def list_collection_copies(collection_id: int, db: Session = Depends(get_db)):
    if not db.query(models.Collection).get(collection_id):
        raise HTTPException(404, "Collection not found")
    copies = db.query(models.Copy).filter(models.Copy.collection_id == collection_id).all()
    return [
        schemas.CopyWithCard(**schemas.CopyOut.model_validate(c).model_dump(), card=utils.card_with_price(db, c.card))
        for c in copies
    ]


@router.get("/{collection_id}/value", response_model=schemas.CollectionValueOut)
def collection_value(collection_id: int, db: Session = Depends(get_db)):
    c = db.query(models.Collection).get(collection_id)
    if not c:
        raise HTTPException(404, "Collection not found")

    copies = db.query(models.Copy).filter(models.Copy.collection_id == collection_id).all()
    total_sell = 0
    total_buy = 0
    total_cost = 0
    for copy in copies:
        latest = (
            db.query(models.PriceSnapshot)
            .filter(models.PriceSnapshot.card_id == copy.card_id)
            .order_by(models.PriceSnapshot.scraped_at.desc())
            .first()
        )
        if latest:
            total_sell += latest.sell_price_jpy or 0
            total_buy += latest.buy_price_jpy or 0
        total_cost += copy.purchase_price_jpy or 0

    return schemas.CollectionValueOut(
        collection_id=collection_id,
        name=c.name,
        total_copies=len(copies),
        total_sell_value_jpy=total_sell,
        total_buy_value_jpy=total_buy,
        total_purchase_cost_jpy=total_cost,
    )


@router.post("/{collection_id}/price-check", response_model=schemas.PriceCheckResult)
def price_check_collection(collection_id: int, db: Session = Depends(get_db)):
    """Fetches a price only for cards in this collection that don't have
    one yet — fast, catches up new additions without re-checking the rest."""
    if not db.query(models.Collection).get(collection_id):
        raise HTTPException(404, "Collection not found")
    copies = db.query(models.Copy).filter(models.Copy.collection_id == collection_id).all()
    cards = [c.card for c in copies]
    try:
        result = scrape_bridge.run_price_check(db, cards)
    except Exception as e:
        raise HTTPException(502, f"Price check failed: {e}")
    return schemas.PriceCheckResult(**result)


@router.post("/{collection_id}/price-update", response_model=schemas.PriceUpdateResult)
def price_update_collection(collection_id: int, db: Session = Depends(get_db)):
    """Re-checks EVERY card in this collection regardless of whether it
    already has a price, and reports which ones' sell/buy price changed."""
    if not db.query(models.Collection).get(collection_id):
        raise HTTPException(404, "Collection not found")
    copies = db.query(models.Copy).filter(models.Copy.collection_id == collection_id).all()
    cards = [c.card for c in copies]
    try:
        result = scrape_bridge.run_price_update(db, cards)
    except Exception as e:
        raise HTTPException(502, f"Price update failed: {e}")
    return schemas.PriceUpdateResult(**result)
