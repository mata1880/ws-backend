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


def add_game_columns(db: Session):
    """
    Multi-game support: collections/wishlists/binders get a `game` (existing
    rows become "ws"), and cards get a `language` (existing rows become "ja").
    Must run before ANY other migration touches these models through the ORM,
    since the ORM selects every declared column. Safe to run every startup.
    """
    for table, col, default in (("collections", "game", "ws"), ("wishlists", "game", "ws"),
                                ("binders", "game", "ws"), ("cards", "language", "ja")):
        try:
            db.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} VARCHAR DEFAULT '{default}'"))
            db.commit()
        except Exception:
            db.rollback()  # already there
        db.execute(text(f"UPDATE {table} SET {col} = '{default}' WHERE {col} IS NULL"))
        db.commit()


def add_copy_profile_id_column(db: Session):
    """
    Just adds copies.profile_id — nothing else. Deliberately its own tiny
    migration, run BEFORE anything else: migrate_legacy_binder_placements
    (below) already does an ORM query against Copy, and once profile_id
    is declared on the Copy model, EVERY ORM query against it — including
    that unrelated one — implicitly selects that column too, so it has
    to actually exist in the database before any of them run. Same class
    of bug as the sort_order/profile_id ordering issue from the
    collections/wishlists/binders migration; this is what prevents it
    from happening again here.
    """
    try:
        db.execute(text("ALTER TABLE copies ADD COLUMN profile_id INTEGER REFERENCES profiles(id)"))
        db.commit()
    except Exception:
        db.rollback()  # column already exists


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


def add_profiles(db: Session):
    """
    Phase 1 of profile support: adds the profiles table, adds a
    profile_id column to collections/wishlists/binders, and assigns
    every EXISTING row to one starter profile ("Mata", admin) so nothing
    is orphaned. Nothing is enforced yet — every endpoint still works
    exactly as before, unauthenticated, until a later phase turns that
    on. Safe to run on every startup.

    pin_hash is deliberately left null here — a PIN only ever gets set
    by someone typing it into the app's own login screen, never written
    by a migration.
    """
    # The profiles TABLE itself is created automatically by create_all()
    # in main.py, since it's a brand new table — nothing to do for that
    # part here. This function only needs to handle the parts create_all()
    # can't: new columns on EXISTING tables, and dropping the old
    # global-uniqueness constraints those tables had on `name` alone
    # (now replaced by a per-profile uniqueness constraint instead).

    for table in ("collections", "wishlists", "binders"):
        try:
            db.execute(text(f"ALTER TABLE {table} ADD COLUMN profile_id INTEGER REFERENCES profiles(id)"))
            db.commit()
        except Exception:
            db.rollback()  # column already exists

        # Drop whatever the old single-column UNIQUE constraint on `name`
        # actually got auto-named (Postgres picks this itself, so it's
        # looked up rather than guessed), now that name uniqueness is
        # meant to be per-profile instead of global.
        try:
            constraint_name = db.execute(text("""
                SELECT tc.constraint_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.constraint_column_usage ccu
                  ON tc.constraint_name = ccu.constraint_name
                  AND tc.table_name = ccu.table_name
                WHERE tc.table_name = :table
                  AND tc.constraint_type = 'UNIQUE'
                  AND ccu.column_name = 'name'
                LIMIT 1
            """), {"table": table}).scalar()
            if constraint_name:
                db.execute(text(f'ALTER TABLE {table} DROP CONSTRAINT "{constraint_name}"'))
                db.commit()
        except Exception:
            db.rollback()  # already dropped, or this isn't Postgres — fine either way

    # One starter profile owns everything that exists so far. Only ever
    # created once — if "Mata" already exists, or ANY profile already
    # exists, this is a no-op (covers both a plain re-run, and the case
    # where profiles now exist because someone's actually registered).
    if db.query(models.Profile).count() == 0:
        starter = models.Profile(username="Mata", is_admin=True, pin_hash=None)
        db.add(starter)
        db.commit()
        db.refresh(starter)
        print(f"[migration] Created starter admin profile 'Mata' (id={starter.id}) — PIN not set yet.")

        for model in (models.Collection, models.Wishlist, models.Binder):
            db.query(model).filter(model.profile_id.is_(None)).update({"profile_id": starter.id})
        db.commit()

    # Copy is backfilled separately, and on EVERY startup rather than only
    # the first — unlike Collection/Wishlist/Binder, copies.py hasn't
    # necessarily been updated yet to set profile_id when creating a new
    # one, so this keeps self-healing anything that slips through with a
    # null profile_id until that catches up too.
    default_profile = db.query(models.Profile).order_by(models.Profile.id).first()
    if default_profile:
        db.query(models.Copy).filter(models.Copy.profile_id.is_(None)).update({"profile_id": default_profile.id})
        db.commit()

