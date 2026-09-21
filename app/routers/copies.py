from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from .auth import get_current_profile
from .binders import FRAME_COMPATIBILITY, _resolved_layout

router = APIRouter(prefix="/copies", tags=["copies"])


def _owned_copy(db: Session, copy_id: int, profile: models.Profile) -> models.Copy:
    copy = db.query(models.Copy).filter(
        models.Copy.id == copy_id, models.Copy.profile_id == profile.id
    ).first()
    if not copy:
        raise HTTPException(404, "Copy not found")
    return copy


def _owned_collection_id(db: Session, collection_id: int, profile: models.Profile) -> int:
    """Validates a collection_id belongs to this profile before it's used
    to file a copy — 404 either way, same reasoning as elsewhere: a
    collection that exists but belongs to someone else should look
    identical to one that doesn't exist at all."""
    if not db.query(models.Collection).filter(
        models.Collection.id == collection_id, models.Collection.profile_id == profile.id
    ).first():
        raise HTTPException(404, "Collection not found")
    return collection_id


@router.post("", response_model=schemas.CopyOut, status_code=201)
def create_copy(body: schemas.CopyCreate, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """The '+' button on Browse/Collection: adds a brand new physical copy.
    frame_type defaults from the card's rarity (sleeve for bulk-common
    rarities, toploader otherwise) unless explicitly given."""
    card = db.query(models.Card).get(body.card_id)
    if not card:
        raise HTTPException(404, "Card not found")
    if body.collection_id is not None:
        _owned_collection_id(db, body.collection_id, profile)

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
        profile_id=profile.id,
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
def copies_for_card(card_id: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """All of YOUR copies of one card, across every collection — used to
    build the stacked-tile copy dropdown (grade/note editing, 'which
    copy' pickers)."""
    return (
        db.query(models.Copy)
        .filter(models.Copy.card_id == card_id, models.Copy.profile_id == profile.id)
        .order_by(models.Copy.copy_number)
        .all()
    )


@router.get("/counts-for-card/{card_id}", response_model=List[schemas.CollectionCopyCount])
def counts_for_card(card_id: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """
    How many copies of this one card sit in EACH of YOUR collections —
    every one of your collections listed, zero-filled where there are
    none. Powers the inline -/+ stepper directly on each row of the "add
    to collection" picker, so adjusting quantity doesn't need a second
    popup.
    """
    collections = (
        db.query(models.Collection)
        .filter(models.Collection.profile_id == profile.id)
        .order_by(models.Collection.sort_order, models.Collection.name)
        .all()
    )
    counts = dict(
        db.query(models.Copy.collection_id, func.count(models.Copy.id))
        .filter(
            models.Copy.card_id == card_id,
            models.Copy.profile_id == profile.id,
            models.Copy.collection_id.isnot(None),
        )
        .group_by(models.Copy.collection_id)
        .all()
    )
    return [
        schemas.CollectionCopyCount(collection_id=c.id, name=c.name, count=counts.get(c.id, 0))
        for c in collections
    ]


@router.patch("/{copy_id}", response_model=schemas.CopyOut)
def update_copy(copy_id: int, body: schemas.CopyUpdate, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """
    Covers both the settings-gear edits (grade/frame/note/purchase price)
    and moving a copy between collections. Use clear_collection=true to
    explicitly un-file a copy (collection_id alone can't mean that, since
    omitting the field also looks like None in JSON).
    """
    copy = _owned_copy(db, copy_id, profile)

    if body.clear_collection:
        copy.collection_id = None
    elif body.collection_id is not None:
        _owned_collection_id(db, body.collection_id, profile)
        copy.collection_id = body.collection_id

    if body.frame_type is not None:
        if body.frame_type not in models.VALID_FRAME_TYPES:
            raise HTTPException(422, f"frame_type must be one of {models.VALID_FRAME_TYPES}")
        current_slot = db.query(models.BinderSlot).filter(models.BinderSlot.copy_id == copy.id).first()
        if current_slot is not None:
            binder = db.query(models.Binder).get(current_slot.binder_id)
            if binder and body.frame_type not in FRAME_COMPATIBILITY[_resolved_layout(binder.layout)]:
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
def delete_copy(copy_id: int, db: Session = Depends(get_db), profile: models.Profile = Depends(get_current_profile)):
    """
    Removes a copy entirely (e.g. sold). If it was linked to a binder
    slot, that slot is NOT deleted — it just reverts to "planned" (no
    copy_id, always greyed), exactly like a placeholder you never owned a
    copy for yet. That's the whole point of a slot being independent of
    ownership: selling a card off leaves a reminder of where it goes,
    same as removing it from a collection already did before binders
    supported planned placements.
    """
    copy = _owned_copy(db, copy_id, profile)
    slot = db.query(models.BinderSlot).filter(models.BinderSlot.copy_id == copy.id).first()
    if slot is not None:
        slot.copy_id = None
    db.delete(copy)
    db.commit()
