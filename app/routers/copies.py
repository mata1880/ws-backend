from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from .binders import FRAME_COMPATIBILITY, PAGE_SIZE

router = APIRouter(prefix="/copies", tags=["copies"])


@router.post("", response_model=schemas.CopyOut, status_code=201)
def create_copy(body: schemas.CopyCreate, db: Session = Depends(get_db)):
    """The '+' button on Browse/Collection: adds a brand new physical copy.
    frame_type defaults from the card's rarity (sleeve for bulk-common
    rarities, toploader otherwise) unless explicitly given."""
    card = db.query(models.Card).get(body.card_id)
    if not card:
        raise HTTPException(404, "Card not found")
    if body.collection_id is not None and not db.query(models.Collection).get(body.collection_id):
        raise HTTPException(404, "Collection not found")

    frame_type = body.frame_type or models.default_frame_type(card.rarity)
    if frame_type not in models.VALID_FRAME_TYPES:
        raise HTTPException(422, f"frame_type must be one of {models.VALID_FRAME_TYPES}")

    next_number = (
        db.query(func.coalesce(func.max(models.Copy.copy_number), 0))
        .filter(models.Copy.card_id == body.card_id)
        .scalar()
    ) + 1

    copy = models.Copy(
        card_id=body.card_id,
        copy_number=next_number,
        collection_id=body.collection_id,
        grade=body.grade,
        frame_type=frame_type,
        note=body.note,
        purchase_price_jpy=body.purchase_price_jpy,
        date_acquired=body.date_acquired,
    )
    db.add(copy)
    db.commit()
    db.refresh(copy)
    return copy


@router.get("/by-card/{card_id}", response_model=List[schemas.CopyOut])
def copies_for_card(card_id: int, db: Session = Depends(get_db)):
    """All copies of one card, across every collection — used to build the
    stacked-tile copy dropdown (grade/note editing, 'which copy' pickers)."""
    return db.query(models.Copy).filter(models.Copy.card_id == card_id).order_by(models.Copy.copy_number).all()


@router.patch("/{copy_id}", response_model=schemas.CopyOut)
def update_copy(copy_id: int, body: schemas.CopyUpdate, db: Session = Depends(get_db)):
    """
    Covers both the settings-gear edits (grade/frame/note/purchase price)
    and moving a copy between collections. Use clear_collection=true to
    explicitly un-file a copy (collection_id alone can't mean that, since
    omitting the field also looks like None in JSON).
    """
    copy = db.query(models.Copy).get(copy_id)
    if not copy:
        raise HTTPException(404, "Copy not found")

    if body.clear_collection:
        copy.collection_id = None
    elif body.collection_id is not None:
        if not db.query(models.Collection).get(body.collection_id):
            raise HTTPException(404, "Collection not found")
        copy.collection_id = body.collection_id

    if body.frame_type is not None:
        if body.frame_type not in models.VALID_FRAME_TYPES:
            raise HTTPException(422, f"frame_type must be one of {models.VALID_FRAME_TYPES}")
        if copy.binder_id is not None:
            binder = db.query(models.Binder).get(copy.binder_id)
            if binder and body.frame_type not in FRAME_COMPATIBILITY[binder.layout]:
                raise HTTPException(
                    422,
                    f"Can't change to '{body.frame_type}' — it wouldn't fit this copy's "
                    f"current {binder.layout} binder. Remove it from the binder first.",
                )
        copy.frame_type = body.frame_type

    if body.grade is not None:
        copy.grade = body.grade
    if body.note is not None:
        copy.note = body.note
    if body.purchase_price_jpy is not None:
        copy.purchase_price_jpy = body.purchase_price_jpy
    if body.date_acquired is not None:
        copy.date_acquired = body.date_acquired

    db.commit()
    db.refresh(copy)
    return copy


@router.delete("/{copy_id}", status_code=204)
def delete_copy(copy_id: int, db: Session = Depends(get_db)):
    """Removes a copy entirely (e.g. sold) — frees up its binder slot too."""
    copy = db.query(models.Copy).get(copy_id)
    if not copy:
        raise HTTPException(404, "Copy not found")
    db.delete(copy)
    db.commit()


@router.post("/{copy_id}/auto-place", response_model=schemas.BinderSlotOut)
def auto_place(copy_id: int, db: Session = Depends(get_db)):
    """
    Priority-based binder auto-placement: finds the highest-priority binder
    (lowest `priority` number) whose layout allows this copy's frame type,
    preferring an already-grey slot for this exact card (a "planned"
    placeholder from before you owned it) over any other open slot.
    """
    copy = db.query(models.Copy).get(copy_id)
    if not copy:
        raise HTTPException(404, "Copy not found")
    if copy.binder_id is not None:
        raise HTTPException(409, "This copy is already placed in a binder — remove it first")

    binders = db.query(models.Binder).order_by(models.Binder.priority).all()

    for binder in binders:
        if copy.frame_type not in FRAME_COMPATIBILITY[binder.layout]:
            continue

        occupied = {
            c.binder_slot for c in
            db.query(models.Copy).filter(models.Copy.binder_id == binder.id).all()
        }

        grey_match = (
            db.query(models.Copy)
            .filter(
                models.Copy.binder_id == binder.id,
                models.Copy.card_id == copy.card_id,
                models.Copy.collection_id.is_(None),
            )
            .first()
        )
        if grey_match:
            target_slot = grey_match.binder_slot
            grey_match.binder_id = None
            grey_match.binder_slot = None
            db.flush()
            copy.binder_id = binder.id
            copy.binder_slot = target_slot
            db.commit()
            db.refresh(copy)
            return schemas.BinderSlotOut(
                slot_index=target_slot, copy_id=copy.id,
                card=schemas.CardOut.model_validate(copy.card), grade=copy.grade,
                frame_type=copy.frame_type, copy_number=copy.copy_number,
                greyed_out=copy.collection_id is None,
            )

        slot = 0
        while slot in occupied:
            slot += 1
        if slot < PAGE_SIZE[binder.layout] * 50:
            copy.binder_id = binder.id
            copy.binder_slot = slot
            db.commit()
            db.refresh(copy)
            return schemas.BinderSlotOut(
                slot_index=slot, copy_id=copy.id,
                card=schemas.CardOut.model_validate(copy.card), grade=copy.grade,
                frame_type=copy.frame_type, copy_number=copy.copy_number,
                greyed_out=copy.collection_id is None,
            )

    raise HTTPException(409, f"No binder can currently fit a '{copy.frame_type}' copy — create one or free up a slot")
