"""
Database engine + session setup. SQLite by default (one file, zero setup) —
plenty for personal use. Swap DATABASE_URL for a Postgres URL later without
touching any other file, if this ever needs to grow past one user.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./ws_collection.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
# pool_pre_ping: test each pooled connection before handing it to a request,
# and transparently replace it if it's dead. Neon's free tier suspends the
# database after ~5 min idle, which silently kills every open connection —
# without this, the FIRST request after any idle period got a dead one and
# failed, and only the retry worked.
# pool_recycle: also retire connections older than 280s outright, just
# under Neon's 5-minute suspend window, so most never even get that far.
engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
    pool_recycle=280,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
