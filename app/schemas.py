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


class ScrapeCatalogRequest(BaseModel):
    query: str               # e.g. "OSK"


class ScrapeResult(BaseModel):
    cards_seen: int
    price_snapshots_added: int
    sets: List[str] = []


class CollectionCreate(BaseModel):
    name: str


class CollectionRename(BaseModel):
    name: str


class CollectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    created_at: datetime


class CollectionValueOut(BaseModel):
    collection_id: int
    name: str
    total_copies: int
    total_sell_value_jpy: int
    total_buy_value_jpy: int
    total_purchase_cost_jpy: int


class WishlistCreate(BaseModel):
    name: str


class WishlistRename(BaseModel):
    name: str


class WishlistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    created_at: datetime


class WishlistItemAdd(BaseModel):
    card_id: int


class BinderCreate(BaseModel):
    name: str
    layout: str = "3x3"
    priority: int = 0


class BinderUpdate(BaseModel):
    name: Optional[str] = None
    layout: Optional[str] = None
    priority: Optional[int] = None


class BinderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    layout: str
    priority: int
    created_at: datetime


class BinderSlotOut(BaseModel):
    slot_index: int
    copy_id: Optional[int] = None
    card: Optional[CardOut] = None
    grade: Optional[str] = None
    frame_type: Optional[str] = None
    copy_number: Optional[int] = None
    greyed_out: bool = False  # true when the copy exists but isn't in any collection


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


class AssignSlotRequest(BaseModel):
    copy_id: int
