from sqlalchemy.orm import Session

from . import models, schemas


def card_with_price(db: Session, card: models.Card) -> schemas.CardWithPrice:
    """
    Builds a CardWithPrice (name/rarity/text/... + current sell/buy price +
    owned copy count + wishlist membership) from a Card row. Used anywhere
    a card needs to be returned with its price attached — /cards,
    wishlist items, and copies' embedded card — so this logic only lives
    in one place instead of being copy-pasted (which is exactly how the
    "wishlist/collection show no price" bug happened: those two endpoints
    were built with a plain CardOut that never had price fields at all).
    """
    latest = (
        db.query(models.PriceSnapshot)
        .filter(models.PriceSnapshot.card_id == card.id)
        .order_by(models.PriceSnapshot.scraped_at.desc())
        .first()
    )
    owned = db.query(models.Copy).filter(models.Copy.card_id == card.id).count()
    wi = db.query(models.WishlistItem).filter(models.WishlistItem.card_id == card.id).first()
    return schemas.CardWithPrice(
        **schemas.CardOut.model_validate(card).model_dump(),
        sell_price_jpy=latest.sell_price_jpy if latest else None,
        buy_price_jpy=latest.buy_price_jpy if latest else None,
        price_scraped_at=latest.scraped_at if latest else None,
        owned_copies=owned or 0,
        wishlist_id=wi.wishlist_id if wi else None,
    )
