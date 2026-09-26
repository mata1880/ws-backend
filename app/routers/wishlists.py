from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from .. import models, schemas, utils, scrape_bridge
from ..database import get_db
from .auth import get_current_profile

router = APIRouter(prefix="/wishlists", tags=["wishlists"])


def _owned_wishlist(db: Session, wishlist_id: int, profile: models.Profile) -> models.Wishlist:
    w = db.query(models.Wishlist).filter(
        models.Wishlist.id == wishlist_id, models.Wishlist.profile_id == profile.id
    ).first()
    if not w:
        raise HTTPException(404, "Wishlist not found")
    return w


@router.get("", response_model=List[schemas.WishlistOut])
def list_wishlists(game: str = "ws", db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    return (
        db.query(models.Wishlist)
        .filter(models.Wishlist.profile_id == profile.id, models.Wishlist.game == game)
        .order_by(models.Wishlist.sort_order, models.Wishlist.name)
        .all()
    )


@router.put("/reorder", status_code=204)
def reorder_wishlists(body: schemas.ReorderRequest, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    for i, wid in enumerate(body.ids):
        db.query(models.Wishlist).filter(
            models.Wishlist.id == wid, models.Wishlist.profile_id == profile.id
        ).update({"sort_order": i})
    db.commit()


@router.post("", response_model=schemas.WishlistOut, status_code=201)
def create_wishlist(body: schemas.WishlistCreate, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    if db.query(models.Wishlist).filter(
        models.Wishlist.name == body.name, models.Wishlist.profile_id == profile.id
    ).first():
        raise HTTPException(409, "A wishlist with that name already exists")
    max_order = db.query(func.max(models.Wishlist.sort_order)).filter(
        models.Wishlist.profile_id == profile.id
    ).scalar() or 0
    w = models.Wishlist(name=body.name, game=body.game, sort_order=max_order + 1, profile_id=profile.id)
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


@router.patch("/{wishlist_id}", response_model=schemas.WishlistOut)
def rename_wishlist(wishlist_id: int, body: schemas.WishlistRename, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    w = _owned_wishlist(db, wishlist_id, profile)
    w.name = body.name
    db.commit()
    db.refresh(w)
    return w


@router.delete("/{wishlist_id}", status_code=204)
def delete_wishlist(wishlist_id: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    w = _owned_wishlist(db, wishlist_id, profile)
    db.delete(w)
    db.commit()


@router.get("/{wishlist_id}/items", response_model=List[schemas.CardWithPrice])
def list_wishlist_items(wishlist_id: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    _owned_wishlist(db, wishlist_id, profile)
    items = db.query(models.WishlistItem).filter(models.WishlistItem.wishlist_id == wishlist_id).all()
    cards_by_id = utils.cards_with_price_batch(db, [i.card for i in items])
    return [cards_by_id[i.card_id] for i in items]


@router.post("/{wishlist_id}/items", status_code=201)
def add_wishlist_item(wishlist_id: int, body: schemas.WishlistItemAdd, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    _owned_wishlist(db, wishlist_id, profile)
    if not db.query(models.Card).get(body.card_id):
        raise HTTPException(404, "Card not found")

    # A card can only be on ONE of THIS PROFILE'S wishlists at a time —
    # scoped by profile_id via the join, so someone else wishlisting the
    # same card elsewhere is completely unaffected. Adding it here removes
    # any existing membership on one of your OWN other wishlists — this is
    # the only automatic removal that happens; moving off a wishlist
    # entirely is always manual.
    existing = (
        db.query(models.WishlistItem)
        .join(models.Wishlist, models.WishlistItem.wishlist_id == models.Wishlist.id)
        .filter(models.WishlistItem.card_id == body.card_id, models.Wishlist.profile_id == profile.id)
        .first()
    )
    if existing:
        if existing.wishlist_id == wishlist_id:
            return {"ok": True, "already_on_this_wishlist": True}
        db.delete(existing)
        db.flush()

    db.add(models.WishlistItem(wishlist_id=wishlist_id, card_id=body.card_id))
    db.commit()
    return {"ok": True}


@router.delete("/{wishlist_id}/items/{card_id}", status_code=204)
def remove_wishlist_item(wishlist_id: int, card_id: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    _owned_wishlist(db, wishlist_id, profile)
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
def price_check_wishlist(wishlist_id: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """Fetches a price only for cards on this wishlist that don't have
    one yet — fast, catches up new additions without re-checking the rest."""
    _owned_wishlist(db, wishlist_id, profile)
    items = db.query(models.WishlistItem).filter(models.WishlistItem.wishlist_id == wishlist_id).all()
    cards = [i.card for i in items]
    try:
        result = scrape_bridge.run_price_check(db, cards)
    except Exception as e:
        raise HTTPException(502, f"Price check failed: {e}")
    return schemas.PriceCheckResult(**result)


@router.post("/{wishlist_id}/price-update", response_model=schemas.PriceUpdateResult)
def price_update_wishlist(wishlist_id: int, body: schemas.PriceUpdateRequest = schemas.PriceUpdateRequest(), db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """Re-checks EVERY card on this wishlist regardless of whether it
    already has a price, and reports which ones' sell/buy price changed."""
    _owned_wishlist(db, wishlist_id, profile)
    items = db.query(models.WishlistItem).filter(models.WishlistItem.wishlist_id == wishlist_id).all()
    cards = [i.card for i in items]
    try:
        result = scrape_bridge.run_price_update(db, cards, only_titles=body.only_titles)
    except Exception as e:
        raise HTTPException(502, f"Price update failed: {e}")
    return schemas.PriceUpdateResult(**result)
