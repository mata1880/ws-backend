from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas, scrape_bridge
from ..database import get_db

router = APIRouter(prefix="/binders", tags=["binders"])

# Which frame types physically fit in each binder layout — a toploader or
# slab doesn't fit in a thin sleeve-page pocket, and nothing slabbed fits
# in a binder at all. A planned (unowned) slot has no frame_type yet, so
# this only gets checked once a real copy is linked to a slot.
PAGE_SIZE = {"3x3": 9, "4x3": 12}
FRAME_COMPATIBILITY = {
    "3x3": {"raw", "sleeve", "toploader"},
    "4x3": {"raw", "sleeve"},
}
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


def _slot_out(slot: models.BinderSlot) -> schemas.BinderSlotOut:
    copy = slot.copy
    return schemas.BinderSlotOut(
        slot_index=slot.slot_index,
        copy_id=copy.id if copy else None,
        card=schemas.CardOut.model_validate(slot.card),
        grade=copy.grade if copy else None,
        frame_type=copy.frame_type if copy else None,
        copy_number=copy.copy_number if copy else None,
        planned=copy is None,
        greyed_out=(copy is None) or (copy.collection_id is None),
    )


@router.get("", response_model=List[schemas.BinderOut])
def list_binders(db: Session = Depends(get_db)):
    return db.query(models.Binder).order_by(models.Binder.priority).all()


@router.post("", response_model=schemas.BinderOut, status_code=201)
def create_binder(body: schemas.BinderCreate, db: Session = Depends(get_db)):
    _validate_layout(body.layout)
    if db.query(models.Binder).filter(models.Binder.name == body.name).first():
        raise HTTPException(409, "A binder with that name already exists")
    b = models.Binder(name=body.name, layout=body.layout, priority=body.priority)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


@router.patch("/{binder_id}", response_model=schemas.BinderOut)
def update_binder(binder_id: int, body: schemas.BinderUpdate, db: Session = Depends(get_db)):
    b = db.query(models.Binder).get(binder_id)
    if not b:
        raise HTTPException(404, "Binder not found")
    if body.layout is not None:
        _validate_layout(body.layout)
        b.layout = body.layout
    if body.name is not None:
        b.name = body.name
    if body.priority is not None:
        b.priority = body.priority
    db.commit()
    db.refresh(b)
    return b


@router.delete("/{binder_id}", status_code=204)
def delete_binder(binder_id: int, db: Session = Depends(get_db)):
    b = db.query(models.Binder).get(binder_id)
    if not b:
        raise HTTPException(404, "Binder not found")
    db.query(models.BinderSlot).filter(models.BinderSlot.binder_id == binder_id).delete()
    db.delete(b)
    db.commit()


@router.get("/{binder_id}/slots", response_model=List[schemas.BinderSlotOut])
def get_slots(binder_id: int, db: Session = Depends(get_db)):
    """
    Returns only OCCUPIED (or planned) slots — the frontend knows the
    binder's layout and therefore the page size, and renders empty slots
    itself for whatever indices aren't in this list.
    """
    b = db.query(models.Binder).get(binder_id)
    if not b:
        raise HTTPException(404, "Binder not found")
    slots = db.query(models.BinderSlot).filter(models.BinderSlot.binder_id == binder_id).all()
    return sorted([_slot_out(s) for s in slots], key=lambda s: s.slot_index)


@router.post("/{binder_id}/slots/{slot_index}", response_model=schemas.BinderSlotOut)
def assign_slot(binder_id: int, slot_index: int, body: schemas.AssignSlotRequest, db: Session = Depends(get_db)):
    """
    Two ways to fill a slot:
    - copy_id given: link an owned copy (must physically fit this binder's layout).
    - card_id only (no copy_id): a "planned" placeholder for a card you
      don't own yet — always shows greyed out until a copy gets linked
      later (either straight to this slot, or via the send-to-binder
      button elsewhere, which reuses an existing planned slot for that
      card instead of creating a second one).
    """
    b = db.query(models.Binder).get(binder_id)
    if not b:
        raise HTTPException(404, "Binder not found")

    existing = (
        db.query(models.BinderSlot)
        .filter(models.BinderSlot.binder_id == binder_id, models.BinderSlot.slot_index == slot_index)
        .first()
    )

    if body.copy_id is not None:
        copy = db.query(models.Copy).get(body.copy_id)
        if not copy:
            raise HTTPException(404, "Copy not found")
        allowed = FRAME_COMPATIBILITY[_resolved_layout(b.layout)]
        if copy.frame_type not in allowed:
            raise HTTPException(
                422,
                f"A '{copy.frame_type}' copy doesn't physically fit a {b.layout} binder "
                f"(allowed here: {', '.join(sorted(allowed))})",
            )
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
            return _slot_out(existing)
        slot = models.BinderSlot(binder_id=binder_id, slot_index=slot_index, card_id=copy.card_id, copy_id=copy.id)
        db.add(slot)
        db.commit()
        db.refresh(slot)
        return _slot_out(slot)

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
    return _slot_out(slot)


@router.delete("/{binder_id}/slots/{slot_index}", status_code=204)
def unassign_slot(binder_id: int, slot_index: int, db: Session = Depends(get_db)):
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
def available_copies(binder_id: int, card_id: int, db: Session = Depends(get_db)):
    """
    For the '+' button on a binder slot, or the send-to-binder flow:
    which of this card's copies are currently unplaced (not linked to any
    slot) and physically fit this binder's layout.
    """
    b = db.query(models.Binder).get(binder_id)
    if not b:
        raise HTTPException(404, "Binder not found")
    allowed = FRAME_COMPATIBILITY[_resolved_layout(b.layout)]
    placed_copy_ids = {
        row.copy_id for row in
        db.query(models.BinderSlot.copy_id).filter(models.BinderSlot.copy_id.isnot(None))
    }
    copies = db.query(models.Copy).filter(models.Copy.card_id == card_id, models.Copy.frame_type.in_(allowed)).all()
    return [c for c in copies if c.id not in placed_copy_ids]


@router.get("/{binder_id}/planned-slot", response_model=schemas.BinderSlotOut)
def find_planned_slot(binder_id: int, card_id: int, db: Session = Depends(get_db)):
    """Is there already an unfilled (planned) slot for this card in this
    binder? Used by the send-to-binder flow to fill an existing
    placeholder instead of creating a duplicate one. 404 if none."""
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
    return _slot_out(slot)


@router.post("/{binder_id}/price-check", response_model=schemas.PriceCheckResult)
def price_check_binder(binder_id: int, db: Session = Depends(get_db)):
    """Re-scrapes current prices for every card currently placed (owned or
    planned) in this binder."""
    if not db.query(models.Binder).get(binder_id):
        raise HTTPException(404, "Binder not found")
    slots = db.query(models.BinderSlot).filter(models.BinderSlot.binder_id == binder_id).all()
    cards = [s.card for s in slots]
    try:
        result = scrape_bridge.run_price_check(db, cards)
    except Exception as e:
        raise HTTPException(502, f"Price check failed: {e}")
    return schemas.PriceCheckResult(**result)
