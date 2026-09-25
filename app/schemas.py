from datetime import datetime, date
from typing import Optional, List

from pydantic import BaseModel, ConfigDict


class CardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    game: str
    set_code: str
    card_number: str
    name: str
    rarity: Optional[str] = None
    image_url: Optional[str] = None
    expansion_name: Optional[str] = None
    title_number: Optional[str] = None
    color: Optional[str] = None
    level: Optional[str] = None
    cost: Optional[str] = None
    power: Optional[str] = None
    soul: Optional[str] = None
    trigger: Optional[str] = None
    traits: Optional[str] = None
    text: Optional[str] = None
    flavor: Optional[str] = None


class CardWithPrice(CardOut):
    sell_price_jpy: Optional[int] = None
    buy_price_jpy: Optional[int] = None
    price_scraped_at: Optional[datetime] = None
    owned_copies: int = 0
    wishlist_id: Optional[int] = None  # which wishlist this card is currently on, if any
    availability: Optional[str] = None  # e.g. "In Stock" / "Sold Out", from the latest price snapshot
    sell_trend: Optional[str] = None  # "up" / "down" / "same" vs the previous snapshot, or null if there's no previous one
    buy_trend: Optional[str] = None
    in_binder: bool = False  # true if a slot for this card exists in ANY binder, even a greyed one


class PriceSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    scraped_at: datetime
    sell_price_jpy: Optional[int] = None
    buy_price_jpy: Optional[int] = None
    buy_price_boosted: bool = False
    stock: Optional[int] = None
    availability: Optional[str] = None


class ScrapePricesRequest(BaseModel):
    game: str = "ws"
    card_code: str          # e.g. "OSK/S133" or "OSK" — matches --card-code
    mode: str = "both"      # "sell" | "buy" | "both"
    skip_bulk_rarities: bool = False  # if true, still adds/updates the cards but doesn't fetch prices for C/U/R/CR/CX


class ScrapeCatalogRequest(BaseModel):
    query: str               # e.g. "OSK"


class ScrapeResult(BaseModel):
    cards_seen: int
    price_snapshots_added: int
    sets: List[str] = []


class PriceCheckResult(BaseModel):
    prefixes_checked: List[str]
    total_price_snapshots_added: int
    truncated: bool


class PriceChangeItem(BaseModel):
    card_number: str
    name: str
    old_sell_price_jpy: Optional[int] = None
    new_sell_price_jpy: Optional[int] = None
    old_buy_price_jpy: Optional[int] = None
    new_buy_price_jpy: Optional[int] = None


class PriceUpdateRequest(BaseModel):
    only_titles: Optional[List[str]] = None  # process exactly these titles (for incremental per-title progress); omit for the default auto-grouped/capped behavior


class PriceUpdateResult(BaseModel):
    checked: int
    changed: List[PriceChangeItem]
    total_price_snapshots_added: int
    truncated: bool


class CollectionCreate(BaseModel):
    name: str


class CollectionRename(BaseModel):
    name: str


class CollectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    created_at: datetime
    sort_order: int = 0


class CollectionValueOut(BaseModel):
    collection_id: int
    name: str
    total_copies: int
    total_sell_value_jpy: int
    total_buy_value_jpy: int
    total_purchase_cost_jpy: int
    # Profit only compares like with like: the sell value of the copies that
    # actually HAVE a paid price, minus what was paid for them.
    costed_copies: int = 0
    costed_sell_value_jpy: int = 0
    profit_jpy: int = 0


class BinderValueOut(BaseModel):
    binder_id: int
    name: str
    counted_slots: int    # owned + collection-filed slots only — greyed/planned ones don't count toward owned_*
    total_slots: int       # counted_slots + greyed/planned ones, for reference
    owned_sell_value_jpy: int
    owned_buy_value_jpy: int
    greyed_sell_value_jpy: int  # value of what's greyed out (planned, or owned-but-not-filed) — what you're missing
    greyed_buy_value_jpy: int
    total_sell_value_jpy: int   # owned + greyed combined
    total_buy_value_jpy: int


class WishlistCreate(BaseModel):
    name: str


class WishlistRename(BaseModel):
    name: str


class WishlistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    created_at: datetime
    sort_order: int = 0


class WishlistItemAdd(BaseModel):
    card_id: int


class BinderCreate(BaseModel):
    name: str
    layout: str = "3x3"


class BinderUpdate(BaseModel):
    name: Optional[str] = None
    layout: Optional[str] = None


class BinderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    layout: str
    created_at: datetime
    sort_order: int = 0


class ReorderRequest(BaseModel):
    ids: List[int]  # the FULL list of IDs for this type, in the desired display order


class BinderPageLabelOut(BaseModel):
    page_number: int
    name: str


class BinderPageLabelSet(BaseModel):
    name: str


class BinderSlotOut(BaseModel):
    slot_index: int
    copy_id: Optional[int] = None
    card: Optional[CardOut] = None
    grade: Optional[str] = None
    frame_type: Optional[str] = None
    copy_number: Optional[int] = None
    greyed_out: bool = False  # true when there's no linked, collection-filed copy yet
    planned: bool = False     # true when this slot has no copy at all (card you don't own)
    wishlist_id: Optional[int] = None  # which wishlist this card is currently on, if any


class FillableSlotOut(BaseModel):
    slot_index: int
    card: CardOut
    copy_id: int  # the specific owned, collection-filed copy that would fill this slot


class FillAllResult(BaseModel):
    filled: int
    slots: List[int]  # slot_index values that got filled


class CopyCreate(BaseModel):
    card_id: int
    collection_id: Optional[int] = None
    grade: Optional[str] = None
    frame_type: Optional[str] = None  # omit to auto-pick from the card's rarity (sleeve vs toploader)
    note: Optional[str] = None
    purchase_price_jpy: Optional[int] = None
    date_acquired: Optional[date] = None


class CopyUpdate(BaseModel):
    collection_id: Optional[int] = None
    clear_collection: bool = False   # explicit flag, since collection_id=None is ambiguous with "don't change"
    grade: Optional[str] = None
    frame_type: Optional[str] = None
    note: Optional[str] = None
    purchase_price_jpy: Optional[int] = None
    date_acquired: Optional[date] = None


class CopyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    card_id: int
    copy_number: int
    collection_id: Optional[int] = None
    binder_id: Optional[int] = None
    binder_slot: Optional[int] = None
    grade: Optional[str] = None
    frame_type: str
    note: Optional[str] = None
    purchase_price_jpy: Optional[int] = None
    date_acquired: Optional[date] = None


class CopyWithCard(CopyOut):
    card: CardWithPrice


class AssignSlotRequest(BaseModel):
    copy_id: Optional[int] = None  # link an owned copy to this slot
    card_id: Optional[int] = None  # or: place a "planned" slot for a card you don't own yet (no copy_id)


class MoveSlotRequest(BaseModel):
    from_index: int
    to_index: int


class CollectionCopyCount(BaseModel):
    collection_id: int
    name: str
    count: int


class LoginRequest(BaseModel):
    username: str
    pin: str


class LoginResult(BaseModel):
    token: str
    profile_id: int
    username: str
    is_admin: bool
    newly_set: bool  # true if this call just SET the PIN for the first time, rather than verifying an existing one


class MeResult(BaseModel):
    profile_id: int
    username: str
    is_admin: bool


class CreateProfileRequest(BaseModel):
    username: str


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    is_admin: bool
    has_pin: bool  # whether they've logged in and set their PIN yet, without exposing the hash itself
