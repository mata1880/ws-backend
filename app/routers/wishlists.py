from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas, utils, scrape_bridge
from ..database import get_db

router = APIRouter(prefix="/wishlists", tags=["wishlists"])


@router.get("", response_model=List[schemas.WishlistOut])
def list_wishlists(db: Session = Depends(get_db)):
    return db.query(models.Wishlist).order_by(models.Wishlist.name).all()


@router.post("", response_model=schemas.WishlistOut, status_code=201)
def create_wishlist(body: schemas.WishlistCreate, db: Session = Depends(get_db)):
    if db.query(models.Wishlist).filter(models.Wishlist.name == body.name).first():
        raise HTTPException(409, "A wishlist with that name already exists")
    w = models.Wishlist(name=body.name)
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


@router.patch("/{wishlist_id}", response_model=schemas.WishlistOut)
def rename_wishlist(wishlist_id: int, body: schemas.WishlistRename, db: Session = Depends(get_db)):
    w = db.query(models.Wishlist).get(wishlist_id)
    if not w:
        raise HTTPException(404, "Wishlist not found")
    w.name = body.name
    db.commit()
    db.refresh(w)
    return w


@router.delete("/{wishlist_id}", status_code=204)
def delete_wishlist(wishlist_id: int, db: Session = Depends(get_db)):
    w = db.query(models.Wishlist).get(wishlist_id)
    if not w:
        raise HTTPException(404, "Wishlist not found")
    db.delete(w)
    db.commit()


@router.get("/{wishlist_id}/items", response_model=List[schemas.CardWithPrice])
def list_wishlist_items(wishlist_id: int, db: Session = Depends(get_db)):
    if not db.query(models.Wishlist).get(wishlist_id):
        raise HTTPException(404, "Wishlist not found")
    items = db.query(models.WishlistItem).filter(models.WishlistItem.wishlist_id == wishlist_id).all()
    return [utils.card_with_price(db, i.card) for i in items]


@router.post("/{wishlist_id}/items", status_code=201)
def add_wishlist_item(wishlist_id: int, body: schemas.WishlistItemAdd, db: Session = Depends(get_db)):
    if not db.query(models.Wishlist).get(wishlist_id):
        raise HTTPException(404, "Wishlist not found")
    if not db.query(models.Card).get(body.card_id):
        raise HTTPException(404, "Card not found")

    # A card can only be on ONE wishlist at a time. Adding it here removes
    # any existing membership elsewhere — this is the only automatic
    # removal that happens; moving off a wishlist entirely is always manual.
    existing = db.query(models.WishlistItem).filter(models.WishlistItem.card_id == body.card_id).first()
    if existing:
        if existing.wishlist_id == wishlist_id:
            return {"ok": True, "already_on_this_wishlist": True}
        db.delete(existing)
        db.flush()

    db.add(models.WishlistItem(wishlist_id=wishlist_id, card_id=body.card_id))
    db.commit()
    return {"ok": True}


@router.delete("/{wishlist_id}/items/{card_id}", status_code=204)
def remove_wishlist_item(wishlist_id: int, card_id: int, db: Session = Depends(get_db)):
    item = (
        db.query(models.WishlistItem)
        .filter(models.WishlistItem.wishlist_id == wishlist_id, models.WishlistItem.card_id == card_id)
        .first()
    )
    if not item:
        raise HTTPException(404, "Not on this wishlist")
    db.delete(item)
    db.commit()


@router.post("/{wishlist_id}/price-check", response_model=schemas.PriceCheckResult)
def price_check_wishlist(wishlist_id: int, db: Session = Depends(get_db)):
    """Fetches a price only for cards on this wishlist that don't have
    one yet — fast, catches up new additions without re-checking the rest."""
    if not db.query(models.Wishlist).get(wishlist_id):
        raise HTTPException(404, "Wishlist not found")
    items = db.query(models.WishlistItem).filter(models.WishlistItem.wishlist_id == wishlist_id).all()
    cards = [i.card for i in items]
    try:
        result = scrape_bridge.run_price_check(db, cards)
    except Exception as e:
        raise HTTPException(502, f"Price check failed: {e}")
    return schemas.PriceCheckResult(**result)


@router.post("/{wishlist_id}/price-update", response_model=schemas.PriceUpdateResult)
def price_update_wishlist(wishlist_id: int, db: Session = Depends(get_db)):
    """Re-checks EVERY card on this wishlist regardless of whether it
    already has a price, and reports which ones' sell/buy price changed."""
    if not db.query(models.Wishlist).get(wishlist_id):
        raise HTTPException(404, "Wishlist not found")
    items = db.query(models.WishlistItem).filter(models.WishlistItem.wishlist_id == wishlist_id).all()
    cards = [i.card for i in items]
    try:
        result = scrape_bridge.run_price_update(db, cards)
    except Exception as e:
        raise HTTPException(502, f"Price update failed: {e}")
    return schemas.PriceUpdateResult(**result)
