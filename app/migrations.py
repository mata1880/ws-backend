"""
Runs at startup. Only does real work once — safe to run on every boot.

Converts any pre-existing Copy.binder_id/binder_slot placements (from
before BinderSlot existed) into proper BinderSlot rows, so upgrading to
the new placeholder-slot system doesn't wipe out binders you'd already
arranged.
"""
from sqlalchemy.orm import Session
from sqlalchemy import text

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


def add_sort_order_columns(db: Session):
    """
    Adds a sort_order column to collections/wishlists/binders if it
    doesn't already exist yet, for the sidebar's manual reorder feature.
    create_all() only creates missing TABLES, not missing COLUMNS on
    tables that already exist, hence this explicit step. Safe to run
    every startup: the ALTER TABLE just fails harmlessly (caught and
    ignored) once the column is already there.
    """
    tables_and_models = [
        ("collections", models.Collection),
        ("wishlists", models.Wishlist),
        ("binders", models.Binder),
    ]
    for table, model in tables_and_models:
        try:
            db.execute(text(f"ALTER TABLE {table} ADD COLUMN sort_order INTEGER DEFAULT 0"))
            db.commit()
        except Exception:
            db.rollback()  # column already exists — nothing to do

        # Backfill a stable initial order, but only if this table has
        # never been manually reordered yet (every row still shares the
        # same default value) — otherwise a restart would silently wipe
        # out someone's custom ordering.
        total = db.query(model).count()
        distinct_values = db.query(model.sort_order).distinct().count()
        if total > 1 and distinct_values <= 1:
            rows = db.query(model).order_by(model.id).all()
            for i, row in enumerate(rows):
                row.sort_order = i
            db.commit()

