"""
Data model. Design notes (matching the brainstorm this came out of):

- A `Card` is one distinct printing (yuyu-tei's card_number already encodes
  rarity, e.g. "OSK/S133-001SSP" — that's the natural unique key).
- `PriceSnapshot` rows are NEVER overwritten — every scrape adds a new row,
  so price history and "value over time" just fall out of a query. Current
  price = latest snapshot per card.
- `Copy` is the actual unit of truth for ownership. A card with 3 owned
  copies is 3 Copy rows, not a quantity column. Each copy:
    - belongs to at most one Collection (collection_id nullable — a copy
      with no collection is "owned but unfiled", which is exactly the
      trigger for showing grey in a binder).
    - occupies at most one Binder slot (binder_id + slot_index nullable).
    - carries its own grade / frame_type / note / purchase price — these
      are properties of the physical copy, not the card.
- `Wishlist` has no copies/quantity, just membership. A card can be on at
  most one wishlist at a time (enforced by a unique index on card_id in
  wishlist_items), and removal is manual only — never automatic.
- `Binder` has a layout (3x3 / 4x3) which only sets cards-per-page — any
  copy, whatever its frame_type, can be placed in any binder.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, Date, ForeignKey,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import relationship

from .database import Base


def now_utc():
    return datetime.now(timezone.utc)


class Profile(Base):
    """
    A person using this app. pin_hash is null until they've actually set
    their PIN themselves through the app's own login/setup screen — never
    written directly by anyone else, including via a migration script.
    is_admin gates Prices/Price Update and unlimited Price Check.
    """
    __tablename__ = "profiles"

    id = Column(Integer, primary_key=True)
    username = Column(String, nullable=False, unique=True)
    email = Column(String, nullable=True)
    pin_hash = Column(String, nullable=True)
    is_admin = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=now_utc)


class Session(Base):
    """A logged-in session. No expiration by design (agreed: stay logged
    in until an explicit logout) — stored in the database rather than
    in memory so it survives Render's free-tier cold starts."""
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)
    token = Column(String, nullable=False, unique=True, index=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False)
    created_at = Column(DateTime, default=now_utc)


class Card(Base):
    __tablename__ = "cards"

    id = Column(Integer, primary_key=True)
    game = Column(String, nullable=False, default="ws")
    language = Column(String, nullable=False, default="ja")   # "ja" / "zh" / "en" — tagged now, no UI yet
    set_code = Column(String, nullable=False)          # yuyu-tei internal slug, e.g. "osk3.0"
    card_number = Column(String, nullable=False, unique=True)  # e.g. "OSK/S133-001SSP"
    name = Column(String, nullable=False, default="")
    rarity = Column(String, nullable=True)
    image_url = Column(String, nullable=True)

    # Catalog enrichment (from ws-tcg.com) — all optional, filled in by
    # /scrape/catalog. Card text / stats don't change over time the way
    # prices do, so these are just columns on Card, not their own history.
    expansion_name = Column(String, nullable=True)
    title_number = Column(String, nullable=True)
    color = Column(String, nullable=True)
    level = Column(String, nullable=True)
    cost = Column(String, nullable=True)
    power = Column(String, nullable=True)
    soul = Column(String, nullable=True)
    trigger = Column(String, nullable=True)
    traits = Column(String, nullable=True)   # comma-joined
    text = Column(Text, nullable=True)
    flavor = Column(Text, nullable=True)

    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)

    price_snapshots = relationship("PriceSnapshot", back_populates="card", cascade="all, delete-orphan")
    copies = relationship("Copy", back_populates="card")

    __table_args__ = (
        Index("ix_cards_game_set", "game", "set_code"),
    )


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"

    id = Column(Integer, primary_key=True)
    card_id = Column(Integer, ForeignKey("cards.id"), nullable=False)
    scraped_at = Column(DateTime, default=now_utc, nullable=False)
    sell_price_jpy = Column(Integer, nullable=True)
    buy_price_jpy = Column(Integer, nullable=True)
    buy_price_boosted = Column(Boolean, default=False)
    stock = Column(Integer, nullable=True)
    availability = Column(String, nullable=True)

    card = relationship("Card", back_populates="price_snapshots")

    __table_args__ = (
        Index("ix_price_snapshots_card_time", "card_id", "scraped_at"),
    )


class Collection(Base):
    __tablename__ = "collections"
    __table_args__ = (UniqueConstraint("profile_id", "name", name="uq_collection_profile_name"),)

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=True)  # nullable during migration; enforced once auth is on
    game = Column(String, nullable=False, default="ws")   # each game keeps its own collections
    created_at = Column(DateTime, default=now_utc)
    sort_order = Column(Integer, nullable=False, default=0)  # for manual sidebar reordering

    copies = relationship("Copy", back_populates="collection")


class Wishlist(Base):
    __tablename__ = "wishlists"
    __table_args__ = (UniqueConstraint("profile_id", "name", name="uq_wishlist_profile_name"),)

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=True)
    game = Column(String, nullable=False, default="ws")
    created_at = Column(DateTime, default=now_utc)
    sort_order = Column(Integer, nullable=False, default=0)  # for manual sidebar reordering

    items = relationship("WishlistItem", back_populates="wishlist", cascade="all, delete-orphan")


class WishlistItem(Base):
    __tablename__ = "wishlist_items"

    id = Column(Integer, primary_key=True)
    wishlist_id = Column(Integer, ForeignKey("wishlists.id"), nullable=False)
    card_id = Column(Integer, ForeignKey("cards.id"), nullable=False)
    added_at = Column(DateTime, default=now_utc)

    wishlist = relationship("Wishlist", back_populates="items")
    card = relationship("Card")

    __table_args__ = (
        # A card can only be on ONE wishlist at a time, globally — not
        # just unique per-wishlist. Adding it to a new wishlist means the
        # API deletes any existing row for that card_id first.
        UniqueConstraint("card_id", name="uq_wishlist_items_card"),
    )


VALID_LAYOUTS = ("3x3", "4x3")


class Binder(Base):
    __tablename__ = "binders"
    __table_args__ = (UniqueConstraint("profile_id", "name", name="uq_binder_profile_name"),)

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=True)
    game = Column(String, nullable=False, default="ws")
    layout = Column(String, nullable=False, default="3x3")  # one of VALID_LAYOUTS
    priority = Column(Integer, nullable=False, default=0)   # lower = higher priority for auto-placement
    sort_order = Column(Integer, nullable=False, default=0)  # for manual sidebar reordering
    created_at = Column(DateTime, default=now_utc)

    copies = relationship("Copy", back_populates="binder")


# The first five are legacy values (the old Frame setting) and now all mean
# "auto" to the frontend: pick a toploader or magnetic one-touch by rarity.
# The force_* values are explicit overrides from the Collection "Slab" setting.
VALID_FRAME_TYPES = ("raw", "sleeve", "toploader", "one_touch", "slab",
                     "auto", "force_toploader", "force_onetouch", "force_none")

# Bulk/common rarities default to a penny sleeve; anything above that (SR,
# RRR, SP, SSP, SEC, etc.) defaults to a toploader. This only applies when
# a copy is created without an explicit frame_type — an explicit choice
# always wins. Compared case-insensitively since scraped rarity strings
# aren't perfectly consistent in casing across sets.
SLEEVE_RARITIES = {"C", "U", "R", "RR", "CR", "CX"}


def default_frame_type(rarity: str) -> str:
    if not rarity:
        return "sleeve"  # unknown rarity — safer to default to some protection than none
    return "sleeve" if rarity.strip().upper() in SLEEVE_RARITIES else "toploader"


class Sale(Base):
    """
    A permanent record of selling one copy. The copy itself is deleted when
    it's sold (you no longer own it), but this row stays so historic profit
    keeps counting it. purchase_price_jpy is copied over at sale time; when
    it's missing, the sale still counts toward sales but not toward profit.
    """
    __tablename__ = "sales"

    id = Column(Integer, primary_key=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False)
    collection_id = Column(Integer, ForeignKey("collections.id"), nullable=True)  # nulled if the collection is deleted
    card_id = Column(Integer, ForeignKey("cards.id"), nullable=False)
    grade = Column(String, nullable=True)
    purchase_price_jpy = Column(Integer, nullable=True)
    sold_price_jpy = Column(Integer, nullable=False)
    sold_at = Column(DateTime, default=now_utc)


class Copy(Base):
    __tablename__ = "copies"

    id = Column(Integer, primary_key=True)
    card_id = Column(Integer, ForeignKey("cards.id"), nullable=False)
    copy_number = Column(Integer, nullable=False, default=1)  # display order within this card's copies
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=True)  # who owns this physical copy — set directly, since collection_id can be null (unfiled)

    collection_id = Column(Integer, ForeignKey("collections.id"), nullable=True)
    binder_id = Column(Integer, ForeignKey("binders.id"), nullable=True)
    binder_slot = Column(Integer, nullable=True)  # flattened slot index within the binder's grid

    grade = Column(String, nullable=True)                     # e.g. "PSA 10", freeform
    frame_type = Column(String, nullable=False, default="raw")
    note = Column(Text, nullable=True)
    purchase_price_jpy = Column(Integer, nullable=True)
    date_acquired = Column(Date, nullable=True)

    created_at = Column(DateTime, default=now_utc)

    card = relationship("Card", back_populates="copies")
    collection = relationship("Collection", back_populates="copies")
    binder = relationship("Binder", back_populates="copies")

    __table_args__ = (
        # A binder slot can only hold one copy at a time. Kept for
        # backward compatibility with data written before BinderSlot
        # existed — binder placement now lives in BinderSlot instead;
        # these two columns are no longer written to by new code.
        UniqueConstraint("binder_id", "binder_slot", name="uq_copies_binder_slot"),
    )


class BinderSlot(Base):
    """
    One physical pocket in a binder. Deliberately separate from Copy:
    a slot can exist as a placeholder for a card you don't own yet
    (copy_id is null — always shows greyed out), and later gets "filled"
    by linking a real copy once you own one, without ever needing two
    different code paths for "planned" vs "owned" placement.
    """
    __tablename__ = "binder_slots"

    id = Column(Integer, primary_key=True)
    binder_id = Column(Integer, ForeignKey("binders.id"), nullable=False)
    slot_index = Column(Integer, nullable=False)
    card_id = Column(Integer, ForeignKey("cards.id"), nullable=False)
    copy_id = Column(Integer, ForeignKey("copies.id"), nullable=True)
    created_at = Column(DateTime, default=now_utc)

    binder = relationship("Binder")
    card = relationship("Card")
    copy = relationship("Copy")

    __table_args__ = (
        UniqueConstraint("binder_id", "slot_index", name="uq_binderslot_binder_slot"),
        UniqueConstraint("copy_id", name="uq_binderslot_copy"),  # a copy can only fill one slot
    )


class BinderPageLabel(Base):
    """
    An optional custom name for one page of a binder (e.g. "Rare Idols",
    "Signed Cards") — purely for your own reference, doesn't affect what
    can go in that page. Page numbers are 1-indexed and not otherwise
    stored anywhere (a "page" is just a range of slot_index values), so
    this table only ever has rows for pages someone bothered to name.
    """
    __tablename__ = "binder_page_labels"

    id = Column(Integer, primary_key=True)
    binder_id = Column(Integer, ForeignKey("binders.id"), nullable=False)
    page_number = Column(Integer, nullable=False)  # 1-indexed; 1 = cover
    name = Column(String, nullable=False, default="")

    binder = relationship("Binder")

    __table_args__ = (
        UniqueConstraint("binder_id", "page_number", name="uq_binderpagelabel_binder_page"),
    )
