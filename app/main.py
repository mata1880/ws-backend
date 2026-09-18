from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import models, migrations
from .database import engine, SessionLocal
from .routers import cards, scrape, collections, wishlists, binders, copies

models.Base.metadata.create_all(bind=engine)

with SessionLocal() as _db:
    migrations.migrate_legacy_binder_placements(_db)

app = FastAPI(
    title="Weiss Schwarz Collection API",
    description="Personal collection tracker: prices (yuyu-tei), card catalog "
                "(ws-tcg.com), collections, wishlists, binders — all backing "
                "the web/PWA frontend.",
    version="0.1.0",
)

# Wide open for now since this is a single-user personal project. Tighten
# to your actual frontend origin(s) once the PWA has a real domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(cards.router)
app.include_router(scrape.router)
app.include_router(collections.router)
app.include_router(wishlists.router)
app.include_router(binders.router)
app.include_router(copies.router)


@app.get("/")
def root():
    return {"status": "ok", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "ok"}
