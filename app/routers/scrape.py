from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas, scrape_bridge
from ..database import get_db

router = APIRouter(prefix="/scrape", tags=["scrape"])


@router.post("/prices", response_model=schemas.ScrapeResult)
def scrape_prices(req: schemas.ScrapePricesRequest, db: Session = Depends(get_db)):
    """
    Same thing as running `python yuyutei_scraper.py --card-code "..." --mode
    ... --site` locally, but triggered from the app and written straight
    into price history instead of a JSON file.
    """
    try:
        result = scrape_bridge.run_price_scrape(db, req.game, req.card_code, req.mode)
    except Exception as e:
        raise HTTPException(502, f"Scrape failed: {e}")
    return schemas.ScrapeResult(**result)


@router.post("/catalog")
def scrape_catalog(req: schemas.ScrapeCatalogRequest, db: Session = Depends(get_db)):
    """Same as `python wstcg_scraper.py --query "..." --site`, merged into cards directly."""
    try:
        result = scrape_bridge.run_catalog_scrape(db, req.query)
    except Exception as e:
        raise HTTPException(502, f"Scrape failed: {e}")
    return result
