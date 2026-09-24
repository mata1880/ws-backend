from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from .. import models, schemas, scrape_bridge
from ..database import get_db
from .auth import get_current_profile

router = APIRouter(prefix="/binders", tags=["binders"])

# Layout only controls page size now — any copy, whatever its frame type
# (raw, sleeve, toploader, one-touch, slab), can go in any binder.
PAGE_SIZE = {"3x3": 9, "4x3": 12}
# Old layout names (removed) fall back to these so a binder created before
# this change doesn't just start 500-ing — it behaves like the closest
# still-supported layout instead. Edit the binder to move it onto "3x3" or
# "4x3" properly when you get a chance.
_LEGACY_LAYOUT_FALLBACK = {"4x5": "4x3", "5x5": "4x3"}


def _resolved_layout(layout: str) -> str:
    return _LEGACY_LAYOUT_FALLBACK.get(layout, layout)


def _validate_layout(layout: str):
    if layout not in models.VALID_LAYOUTS:
        raise HTTPException(422, f"layout must be one of {models.VALID_LAYOUTS}")


def _owned_binder(db: Session, binder_id: int, profile: models.Profile) -> models.Binder:
    b = db.query(models.Binder).filter(
        models.Binder.id == binder_id, models.Binder.profile_id == profile.id
    ).first()
    if not b:
        raise HTTPException(404, "Binder not found")
    return b


def _slot_out(slot: models.BinderSlot, wishlist_by_card: dict = None, db: Session = None) -> schemas.BinderSlotOut:
    copy = slot.copy
    if wishlist_by_card is not None:
        wishlist_id = wishlist_by_card.get(slot.card_id)
    elif db is not None:
        wi = db.query(models.WishlistItem).filter(models.WishlistItem.card_id == slot.card_id).first()
        wishlist_id = wi.wishlist_id if wi else None
    else:
        wishlist_id = None
    return schemas.BinderSlotOut(
        slot_index=slot.slot_index,
        copy_id=copy.id if copy else None,
        card=schemas.CardOut.model_validate(slot.card),
        grade=copy.grade if copy else None,
        frame_type=copy.frame_type if copy else None,
        copy_number=copy.copy_number if copy else None,
        planned=copy is None,
        greyed_out=(copy is None) or (copy.collection_id is None),
        wishlist_id=wishlist_id,
    )


@router.get("", response_model=List[schemas.BinderOut])
def list_binders(db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    return (
        db.query(models.Binder)
        .filter(models.Binder.profile_id == profile.id)
        .order_by(models.Binder.sort_order, models.Binder.name)
        .all()
    )


@router.put("/reorder", status_code=204)
def reorder_binders(body: schemas.ReorderRequest, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    for i, bid in enumerate(body.ids):
        db.query(models.Binder).filter(
            models.Binder.id == bid, models.Binder.profile_id == profile.id
        ).update({"sort_order": i})
    db.commit()


@router.post("", response_model=schemas.BinderOut, status_code=201)
def create_binder(body: schemas.BinderCreate, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    _validate_layout(body.layout)
    if db.query(models.Binder).filter(
        models.Binder.name == body.name, models.Binder.profile_id == profile.id
    ).first():
        raise HTTPException(409, "A binder with that name already exists")
    max_order = db.query(func.max(models.Binder.sort_order)).filter(
        models.Binder.profile_id == profile.id
    ).scalar() or 0
    b = models.Binder(name=body.name, layout=body.layout, sort_order=max_order + 1, profile_id=profile.id)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


@router.patch("/{binder_id}", response_model=schemas.BinderOut)
def update_binder(binder_id: int, body: schemas.BinderUpdate, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    b = _owned_binder(db, binder_id, profile)
    if body.layout is not None:
        _validate_layout(body.layout)
        b.layout = body.layout
    if body.name is not None:
        b.name = body.name
    db.commit()
    db.refresh(b)
    return b


@router.delete("/{binder_id}", status_code=204)
def delete_binder(binder_id: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    b = _owned_binder(db, binder_id, profile)
    db.query(models.BinderSlot).filter(models.BinderSlot.binder_id == binder_id).delete()
    db.query(models.BinderPageLabel).filter(models.BinderPageLabel.binder_id == binder_id).delete()
    db.delete(b)
    db.commit()


@router.get("/{binder_id}/slots", response_model=List[schemas.BinderSlotOut])
def get_slots(binder_id: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """
    Returns only OCCUPIED (or planned) slots — the frontend knows the
    binder's layout and therefore the page size, and renders empty slots
    itself for whatever indices aren't in this list.
    """
    _owned_binder(db, binder_id, profile)
    slots = db.query(models.BinderSlot).filter(models.BinderSlot.binder_id == binder_id).all()
    card_ids = list({s.card_id for s in slots})
    wishlist_by_card = {}
    if card_ids:
        wishlist_by_card = {
            wi.card_id: wi.wishlist_id
            for wi in db.query(models.WishlistItem).filter(models.WishlistItem.card_id.in_(card_ids)).all()
        }
    return sorted([_slot_out(s, wishlist_by_card=wishlist_by_card) for s in slots], key=lambda s: s.slot_index)


@router.post("/{binder_id}/slots/move", response_model=List[schemas.BinderSlotOut])
def move_slot(binder_id: int, body: schemas.MoveSlotRequest, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """
    Moves (or swaps, if the destination is occupied) a card between two
    slots in ONE request. The frontend used to do this as up to 4
    sequential DELETE/POST round-trips plus a full reload for a single
    move — this is the same two BinderSlot rows, just their slot_index
    updated in place, which is both fewer requests and a cheaper
    operation than deleting and recreating rows.

    Registered BEFORE /slots/{slot_index} below on purpose — FastAPI
    matches routes in registration order, and "move" would otherwise get
    swallowed by that route's {slot_index} path parameter (which is
    exactly the bug that shipped here originally).
    """
    _owned_binder(db, binder_id, profile)

    from_slot = (
        db.query(models.BinderSlot)
        .filter(models.BinderSlot.binder_id == binder_id, models.BinderSlot.slot_index == body.from_index)
        .first()
    )
    if not from_slot:
        raise HTTPException(404, "Source slot is empty")

    to_slot = (
        db.query(models.BinderSlot)
        .filter(models.BinderSlot.binder_id == binder_id, models.BinderSlot.slot_index == body.to_index)
        .first()
    )

    if to_slot is None:
        from_slot.slot_index = body.to_index
        db.commit()
        db.refresh(from_slot)
        wishlist_by_card = {
            wi.card_id: wi.wishlist_id
            for wi in db.query(models.WishlistItem).filter(models.WishlistItem.card_id == from_slot.card_id).all()
        }
        return [_slot_out(from_slot, wishlist_by_card=wishlist_by_card)]

    # Swap: park one slot at a temporary index first, since (binder_id,
    # slot_index) is unique and updating both to each other's index in
    # the wrong order would collide mid-transaction.
    TEMP_INDEX = -1
    from_slot.slot_index = TEMP_INDEX
    db.flush()
    to_slot.slot_index = body.from_index
    db.flush()
    from_slot.slot_index = body.to_index
    db.commit()
    db.refresh(from_slot)
    db.refresh(to_slot)
    wishlist_by_card = {
        wi.card_id: wi.wishlist_id
        for wi in db.query(models.WishlistItem)
        .filter(models.WishlistItem.card_id.in_([from_slot.card_id, to_slot.card_id]))
        .all()
    }
    return [
        _slot_out(from_slot, wishlist_by_card=wishlist_by_card),
        _slot_out(to_slot, wishlist_by_card=wishlist_by_card),
    ]


@router.post("/{binder_id}/slots/{slot_index}", response_model=schemas.BinderSlotOut)
def assign_slot(binder_id: int, slot_index: int, body: schemas.AssignSlotRequest, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """
    Two ways to fill a slot:
    - copy_id given: link an owned copy (any frame type).
    - card_id only (no copy_id): a "planned" placeholder for a card you
      don't own yet — always shows greyed out until a copy gets linked
      later (either straight to this slot, or via the send-to-binder
      button elsewhere, which reuses an existing planned slot for that
      card instead of creating a second one).
    """
    b = _owned_binder(db, binder_id, profile)

    existing = (
        db.query(models.BinderSlot)
        .filter(models.BinderSlot.binder_id == binder_id, models.BinderSlot.slot_index == slot_index)
        .first()
    )

    if body.copy_id is not None:
        copy = db.query(models.Copy).get(body.copy_id)
        if not copy or copy.profile_id != profile.id:
            # Same 404 either way — a copy that exists but belongs to someone
            # else should look identical to one that doesn't exist at all.
            raise HTTPException(404, "Copy not found")
        already_elsewhere = (
            db.query(models.BinderSlot)
            .filter(models.BinderSlot.copy_id == copy.id, models.BinderSlot.id != (existing.id if existing else -1))
            .first()
        )
        if already_elsewhere:
            raise HTTPException(409, "That copy is already placed in a binder slot — remove it from there first")

        if existing:
            existing.copy_id = copy.id
            existing.card_id = copy.card_id
            db.commit()
            db.refresh(existing)
            return _slot_out(existing, db=db)
        slot = models.BinderSlot(binder_id=binder_id, slot_index=slot_index, card_id=copy.card_id, copy_id=copy.id)
        db.add(slot)
        db.commit()
        db.refresh(slot)
        return _slot_out(slot, db=db)

    # Planned placement — no copy_id, just a card.
    if body.card_id is None:
        raise HTTPException(422, "Provide either copy_id or card_id")
    card = db.query(models.Card).get(body.card_id)
    if not card:
        raise HTTPException(404, "Card not found")
    if existing:
        raise HTTPException(409, "That slot is already occupied — unassign it first")
    slot = models.BinderSlot(binder_id=binder_id, slot_index=slot_index, card_id=card.id, copy_id=None)
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return _slot_out(slot, db=db)


@router.delete("/{binder_id}/slots/{slot_index}", status_code=204)
def unassign_slot(binder_id: int, slot_index: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    _owned_binder(db, binder_id, profile)
    slot = (
        db.query(models.BinderSlot)
        .filter(models.BinderSlot.binder_id == binder_id, models.BinderSlot.slot_index == slot_index)
        .first()
    )
    if not slot:
        raise HTTPException(404, "That slot is empty")
    db.delete(slot)
    db.commit()


@router.get("/{binder_id}/available-copies", response_model=List[schemas.CopyOut])
def available_copies(binder_id: int, card_id: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """
    For the '+' button on a binder slot, or the send-to-binder flow:
    which of YOUR copies of this card are currently unplaced (not linked
    to any slot).
    """
    _owned_binder(db, binder_id, profile)
    placed_copy_ids = {
        row.copy_id for row in
        db.query(models.BinderSlot.copy_id).filter(models.BinderSlot.copy_id.isnot(None))
    }
    copies = (
        db.query(models.Copy)
        .filter(models.Copy.card_id == card_id, models.Copy.profile_id == profile.id)
        .all()
    )
    return [c for c in copies if c.id not in placed_copy_ids]


@router.get("/{binder_id}/planned-slot", response_model=schemas.BinderSlotOut)
def find_planned_slot(binder_id: int, card_id: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """Is there already an unfilled (planned) slot for this card in this
    binder? Used by the send-to-binder flow to fill an existing
    placeholder instead of creating a duplicate one. 404 if none."""
    _owned_binder(db, binder_id, profile)
    slot = (
        db.query(models.BinderSlot)
        .filter(
            models.BinderSlot.binder_id == binder_id,
            models.BinderSlot.card_id == card_id,
            models.BinderSlot.copy_id.is_(None),
        )
        .first()
    )
    if not slot:
        raise HTTPException(404, "No planned slot for that card in this binder")
    return _slot_out(slot, db=db)


@router.get("/{binder_id}/page-labels", response_model=List[schemas.BinderPageLabelOut])
def list_page_labels(binder_id: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """Every page in this binder that's been given a custom name."""
    _owned_binder(db, binder_id, profile)
    labels = db.query(models.BinderPageLabel).filter(models.BinderPageLabel.binder_id == binder_id).all()
    return [schemas.BinderPageLabelOut(page_number=l.page_number, name=l.name) for l in labels]


@router.put("/{binder_id}/pages/{page_number}/label", response_model=schemas.BinderPageLabelOut)
def set_page_label(binder_id: int, page_number: int, body: schemas.BinderPageLabelSet, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """Sets (or clears, if name is empty) a page's custom name."""
    _owned_binder(db, binder_id, profile)
    if page_number < 1:
        raise HTTPException(422, "page_number must be 1 or greater")

    label = (
        db.query(models.BinderPageLabel)
        .filter(models.BinderPageLabel.binder_id == binder_id, models.BinderPageLabel.page_number == page_number)
        .first()
    )
    name = body.name.strip()
    if not name:
        if label:
            db.delete(label)
            db.commit()
        return schemas.BinderPageLabelOut(page_number=page_number, name="")

    if label:
        label.name = name
    else:
        label = models.BinderPageLabel(binder_id=binder_id, page_number=page_number, name=name)
        db.add(label)
    db.commit()
    return schemas.BinderPageLabelOut(page_number=page_number, name=name)


@router.get("/{binder_id}/value", response_model=schemas.BinderValueOut)
def binder_value(binder_id: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """
    Sums current sell/buy value for this binder's slots, split into what
    you actually own right now (a real copy linked AND filed into a
    collection) vs. what's greyed out (planned, or owned but not filed
    anywhere) — plus a combined total across both.
    """
    b = _owned_binder(db, binder_id, profile)

    slots = db.query(models.BinderSlot).filter(models.BinderSlot.binder_id == binder_id).all()

    # Latest price per card in ONE query instead of one per slot — this
    # runs after every binder action (loadSlots() always refreshes value).
    card_ids = list({s.card_id for s in slots})
    latest_by_card = {}
    if card_ids:
        rows = (
            db.query(models.PriceSnapshot)
            .filter(models.PriceSnapshot.card_id.in_(card_ids))
            .order_by(models.PriceSnapshot.card_id, models.PriceSnapshot.scraped_at.desc())
            .all()
        )
        for r in rows:
            latest_by_card.setdefault(r.card_id, r)  # first one seen per card_id is the latest, given the ordering

    owned_sell = owned_buy = greyed_sell = greyed_buy = 0
    counted = 0
    for slot in slots:
        latest = latest_by_card.get(slot.card_id)
        sell = latest.sell_price_jpy or 0 if latest else 0
        buy = latest.buy_price_jpy or 0 if latest else 0
        copy = slot.copy
        if copy is not None and copy.collection_id is not None:
            owned_sell += sell
            owned_buy += buy
            counted += 1
        else:
            greyed_sell += sell
            greyed_buy += buy

    return schemas.BinderValueOut(
        binder_id=binder_id, name=b.name, counted_slots=counted, total_slots=len(slots),
        owned_sell_value_jpy=owned_sell, owned_buy_value_jpy=owned_buy,
        greyed_sell_value_jpy=greyed_sell, greyed_buy_value_jpy=greyed_buy,
        total_sell_value_jpy=owned_sell + greyed_sell, total_buy_value_jpy=owned_buy + greyed_buy,
    )


@router.get("/{binder_id}/fillable", response_model=List[schemas.FillableSlotOut])
def fillable_slots(binder_id: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """
    Which of this binder's planned (greyed, no-copy) slots currently have
    an owned, collection-filed copy available to fill them? Drives the
    "you can add X cards to this binder" banner and the Fill-all button —
    entirely scoped to this one binder, no cross-binder priority involved
    (that's been removed: you place cards into binders on purpose now,
    not via automatic priority ordering).
    """
    _owned_binder(db, binder_id, profile)

    placed_copy_ids = {
        row.copy_id for row in
        db.query(models.BinderSlot.copy_id).filter(models.BinderSlot.copy_id.isnot(None))
    }
    planned = (
        db.query(models.BinderSlot)
        .filter(models.BinderSlot.binder_id == binder_id, models.BinderSlot.copy_id.is_(None))
        .all()
    )

    # One query for every candidate copy across ALL planned slots, instead
    # of one query per slot — this runs after every single binder action
    # (loadSlots() always re-checks fillable), so an N+1 here directly
    # slows down every move/remove/fill you do.
    card_ids = list({slot.card_id for slot in planned})
    candidates_by_card = {}
    if card_ids:
        for c in (
            db.query(models.Copy)
            .filter(
                models.Copy.card_id.in_(card_ids),
                models.Copy.profile_id == profile.id,
                models.Copy.collection_id.isnot(None),
            )
            .all()
        ):
            candidates_by_card.setdefault(c.card_id, []).append(c)

    out = []
    for slot in planned:
        candidates = candidates_by_card.get(slot.card_id, [])
        available = next((c for c in candidates if c.id not in placed_copy_ids), None)
        if available:
            out.append(schemas.FillableSlotOut(slot_index=slot.slot_index, card=schemas.CardOut.model_validate(slot.card), copy_id=available.id))
    return out


@router.post("/{binder_id}/fill-all", response_model=schemas.FillAllResult)
def fill_all(binder_id: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """Fills every currently-fillable planned slot in this binder in one go."""
    _owned_binder(db, binder_id, profile)

    filled_slots = []
    # Loop rather than reuse fillable_slots()'s single pass, since filling
    # one slot changes which copies are still available for the next one.
    while True:
        placed_copy_ids = {
            row.copy_id for row in
            db.query(models.BinderSlot.copy_id).filter(models.BinderSlot.copy_id.isnot(None))
        }
        planned = (
            db.query(models.BinderSlot)
            .filter(models.BinderSlot.binder_id == binder_id, models.BinderSlot.copy_id.is_(None))
            .all()
        )
        progressed = False
        for slot in planned:
            candidate = (
                db.query(models.Copy)
                .filter(
                    models.Copy.card_id == slot.card_id,
                    models.Copy.profile_id == profile.id,
                    models.Copy.collection_id.isnot(None),
                )
                .all()
            )
            available = next((c for c in candidate if c.id not in placed_copy_ids), None)
            if available:
                slot.copy_id = available.id
                db.commit()
                filled_slots.append(slot.slot_index)
                progressed = True
                break  # placed_copy_ids is now stale, restart the scan
        if not progressed:
            break

    return schemas.FillAllResult(filled=len(filled_slots), slots=filled_slots)


@router.post("/{binder_id}/price-check", response_model=schemas.PriceCheckResult)
def price_check_binder(binder_id: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """Fetches a price only for cards currently placed in this binder that
    don't have one yet — fast, catches up new additions without re-checking the rest."""
    _owned_binder(db, binder_id, profile)
    slots = db.query(models.BinderSlot).filter(models.BinderSlot.binder_id == binder_id).all()
    cards = [s.card for s in slots]
    try:
        result = scrape_bridge.run_price_check(db, cards)
    except Exception as e:
        raise HTTPException(502, f"Price check failed: {e}")
    return schemas.PriceCheckResult(**result)


@router.post("/{binder_id}/price-update", response_model=schemas.PriceUpdateResult)
def price_update_binder(binder_id: int, body: schemas.PriceUpdateRequest = schemas.PriceUpdateRequest(), db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """Re-checks EVERY card currently placed in this binder regardless of
    whether it already has a price, and reports which ones' sell/buy price changed."""
    _owned_binder(db, binder_id, profile)
    slots = db.query(models.BinderSlot).filter(models.BinderSlot.binder_id == binder_id).all()
    cards = [s.card for s in slots]
    try:
        result = scrape_bridge.run_price_update(db, cards, only_titles=body.only_titles)
    except Exception as e:
        raise HTTPException(502, f"Price update failed: {e}")
    return schemas.PriceUpdateResult(**result)
