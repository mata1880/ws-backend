"""
Runs at startup. Only does real work once — safe to run on every boot.

Converts any pre-existing Copy.binder_id/binder_slot placements (from
before BinderSlot existed) into proper BinderSlot rows, so upgrading to
the new placeholder-slot system doesn't wipe out binders you'd already
arranged.
"""
from sqlalchemy.orm import Session

from . import models


def migrate_legacy_binder_placements(db: Session):
    legacy_copies = (
        db.query(models.Copy)
        .filter(models.Copy.binder_id.isnot(None), models.Copy.binder_slot.isnot(None))
        .all()
    )
    if not legacy_copies:
        return

    already_migrated_copy_ids = {
        row.copy_id for row in db.query(models.BinderSlot.copy_id).filter(models.BinderSlot.copy_id.isnot(None))
    }

    migrated = 0
    for copy in legacy_copies:
        if copy.id in already_migrated_copy_ids:
            continue
        exists_at_slot = (
            db.query(models.BinderSlot)
            .filter(models.BinderSlot.binder_id == copy.binder_id, models.BinderSlot.slot_index == copy.binder_slot)
            .first()
        )
        if exists_at_slot:
            continue
        db.add(models.BinderSlot(
            binder_id=copy.binder_id,
            slot_index=copy.binder_slot,
            card_id=copy.card_id,
            copy_id=copy.id,
        ))
        migrated += 1

    if migrated:
        db.commit()
        print(f"[migration] Converted {migrated} legacy binder placement(s) to BinderSlot rows.")
