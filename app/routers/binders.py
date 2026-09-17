from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db

router = APIRouter(prefix="/binders", tags=["binders"])

# Which frame types physically fit in each binder layout — a toploader or
# slab doesn't fit in a thin sleeve-page pocket, and nothing slabbed fits
# in a binder at all.
PAGE_SIZE = {"3x3": 9, "4x5": 20, "5x5": 25}
FRAME_COMPATIBILITY = {
    "3x3": {"raw", "sleeve", "toploader"},
    "4x5": {"raw", "sleeve"},
    "5x5": {"raw", "sleeve"},
}


def _validate_layout(layout: str):
    if layout not in models.VALID_LAYOUTS:
        raise HTTPException(422, f"layout must be one of {models.VALID_LAYOUTS}")


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
    # Unassign, don't delete, any copies sitting in this binder.
    db.query(models.Copy).filter(models.Copy.binder_id == binder_id).update(
        {"binder_id": None, "binder_slot": None}
    )
    db.delete(b)
    db.commit()


@router.get("/{binder_id}/slots", response_model=List[schemas.BinderSlotOut])
def get_slots(binder_id: int, db: Session = Depends(get_db)):
    """
    Returns only OCCUPIED slots — the frontend knows the binder's layout
    (from the Binder object) and therefore the page size, and renders empty
    slots itself for whatever indices aren't in this list.
    """
    b = db.query(models.Binder).get(binder_id)
    if not b:
        raise HTTPException(404, "Binder not found")

    copies = db.query(models.Copy).filter(models.Copy.binder_id == binder_id).all()
    out = []
    for copy in copies:
        out.append(schemas.BinderSlotOut(
            slot_index=copy.binder_slot,
            copy_id=copy.id,
            card=schemas.CardOut.model_validate(copy.card),
            grade=copy.grade,
            frame_type=copy.frame_type,
            copy_number=copy.copy_number,
            greyed_out=copy.collection_id is None,
        ))
    return sorted(out, key=lambda s: s.slot_index)


@router.post("/{binder_id}/slots/{slot_index}", response_model=schemas.BinderSlotOut)
def assign_slot(binder_id: int, slot_index: int, body: schemas.AssignSlotRequest, db: Session = Depends(get_db)):
    b = db.query(models.Binder).get(binder_id)
    if not b:
        raise HTTPException(404, "Binder not found")
    copy = db.query(models.Copy).get(body.copy_id)
    if not copy:
        raise HTTPException(404, "Copy not found")

    allowed = FRAME_COMPATIBILITY[b.layout]
    if copy.frame_type not in allowed:
        raise HTTPException(
            422,
            f"A '{copy.frame_type}' copy doesn't physically fit a {b.layout} binder "
            f"(allowed here: {', '.join(sorted(allowed))})",
        )

    existing = (
        db.query(models.Copy)
        .filter(models.Copy.binder_id == binder_id, models.Copy.binder_slot == slot_index)
        .first()
    )
    if existing and existing.id != copy.id:
        raise HTTPException(409, "That slot is already occupied — unassign it first")

    copy.binder_id = binder_id
    copy.binder_slot = slot_index
    db.commit()
    db.refresh(copy)

    return schemas.BinderSlotOut(
        slot_index=slot_index, copy_id=copy.id, card=schemas.CardOut.model_validate(copy.card),
        grade=copy.grade, frame_type=copy.frame_type, copy_number=copy.copy_number,
        greyed_out=copy.collection_id is None,
    )


@router.delete("/{binder_id}/slots/{slot_index}", status_code=204)
def unassign_slot(binder_id: int, slot_index: int, db: Session = Depends(get_db)):
    copy = (
        db.query(models.Copy)
        .filter(models.Copy.binder_id == binder_id, models.Copy.binder_slot == slot_index)
        .first()
    )
    if not copy:
        raise HTTPException(404, "That slot is empty")
    copy.binder_id = None
    copy.binder_slot = None
    db.commit()


@router.get("/{binder_id}/available-copies", response_model=List[schemas.CopyOut])
def available_copies(binder_id: int, card_id: int, db: Session = Depends(get_db)):
    """
    For the '+' button on a binder slot: which of this card's copies are
    currently unplaced (not already sitting in any binder) and physically
    fit this binder's layout. The button stays greyed out on the frontend
    when this list comes back empty.
    """
    b = db.query(models.Binder).get(binder_id)
    if not b:
        raise HTTPException(404, "Binder not found")
    allowed = FRAME_COMPATIBILITY[b.layout]
    return (
        db.query(models.Copy)
        .filter(
            models.Copy.card_id == card_id,
            models.Copy.binder_id.is_(None),
            models.Copy.frame_type.in_(allowed),
        )
        .all()
    )
