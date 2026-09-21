from typing import List

import re

from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session as DbSession

from .. import models, schemas
from ..database import get_db
from ..auth import hash_pin, verify_pin, generate_token

router = APIRouter(prefix="/auth", tags=["auth"])

PIN_RE = re.compile(r"^\d{6}$")


@router.post("/login", response_model=schemas.LoginResult)
def login(req: schemas.LoginRequest, db: DbSession = Depends(get_db)):
    """
    Open self-registration, combined with login: a username that doesn't
    exist yet gets created right here (never admin — that's set exactly
    once, by the original migration, and nowhere else), and whatever PIN
    comes with it becomes that profile's PIN from now on. A username
    that already exists but hasn't set a PIN yet behaves the same way —
    this first successful call is what sets it. Every call after that
    just verifies against what got set.
    """
    if not PIN_RE.match(req.pin.strip()):
        raise HTTPException(400, "PIN must be exactly 6 digits.")
    pin = req.pin.strip()
    username = req.username.strip()
    if not username:
        raise HTTPException(422, "Username can't be empty.")

    profile = db.query(models.Profile).filter(models.Profile.username == username).first()
    if not profile:
        profile = models.Profile(username=username, is_admin=False, pin_hash=None)
        db.add(profile)
        db.flush()  # get an id assigned before using it below, without a separate round trip

    newly_set = False
    if profile.pin_hash is None:
        profile.pin_hash = hash_pin(pin)
        newly_set = True
        db.commit()
    else:
        if not verify_pin(pin, profile.pin_hash):
            raise HTTPException(401, "Incorrect PIN.")

    token = generate_token()
    db.add(models.Session(token=token, profile_id=profile.id))
    db.commit()

    return schemas.LoginResult(
        token=token, profile_id=profile.id, username=profile.username,
        is_admin=profile.is_admin, newly_set=newly_set,
    )


def get_current_profile(
    authorization: str = Header(default=None),
    db: DbSession = Depends(get_db),
) -> models.Profile:
    """
    Pulls the token out of an `Authorization: Bearer <token>` header,
    looks up the session, returns the profile it belongs to. Used
    directly by every collections/wishlists/binders/copies endpoint —
    no token, or an invalid one, means a real 401.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or malformed Authorization header.")
    token = authorization.removeprefix("Bearer ").strip()

    session = db.query(models.Session).filter(models.Session.token == token).first()
    if not session:
        raise HTTPException(401, "Invalid or expired session.")

    profile = db.query(models.Profile).get(session.profile_id)
    if not profile:
        raise HTTPException(401, "Session belongs to a profile that no longer exists.")
    return profile


@router.get("/me", response_model=schemas.MeResult)
def me(profile: models.Profile = Depends(get_current_profile)):
    return schemas.MeResult(profile_id=profile.id, username=profile.username, is_admin=profile.is_admin)


@router.post("/logout", status_code=204)
def logout(authorization: str = Header(default=None), db: DbSession = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return  # already not logged in, nothing to do
    token = authorization.removeprefix("Bearer ").strip()
    db.query(models.Session).filter(models.Session.token == token).delete()
    db.commit()


def require_admin(profile: models.Profile = Depends(get_current_profile)) -> models.Profile:
    """Reusable dependency for admin-only endpoints — everything below
    this, plus anything elsewhere that needs the same gate later
    (Prices, unlimited Price Check/Update)."""
    if not profile.is_admin:
        raise HTTPException(403, "Admin only.")
    return profile


@router.post("/profiles", response_model=schemas.ProfileOut, status_code=201)
def create_profile(body: schemas.CreateProfileRequest, db: DbSession = Depends(get_db), admin: models.Profile = Depends(require_admin)):
    """
    Admin-only, deliberately — no public sign-up page exists. Creates the
    USERNAME only; pin_hash stays null, same as every profile does until
    its first successful /auth/login call sets it. Hand the username to
    whoever it's for and have them log in themselves to set their own PIN
    — nobody else's PIN, including yours, ever needs to pass through
    this endpoint or get typed anywhere but the login screen itself.
    """
    username = body.username.strip()
    if not username:
        raise HTTPException(422, "Username can't be empty.")
    if db.query(models.Profile).filter(models.Profile.username == username).first():
        raise HTTPException(409, "That username is already taken.")
    new_profile = models.Profile(username=username, is_admin=False, pin_hash=None)
    db.add(new_profile)
    db.commit()
    db.refresh(new_profile)
    return schemas.ProfileOut(id=new_profile.id, username=new_profile.username, is_admin=new_profile.is_admin, has_pin=False)


@router.get("/profiles", response_model=List[schemas.ProfileOut])
def list_profiles(db: DbSession = Depends(get_db), admin: models.Profile = Depends(require_admin)):
    """Admin-only — every profile that exists, and whether each one has
    actually logged in and set a PIN yet."""
    profiles = db.query(models.Profile).order_by(models.Profile.id).all()
    return [
        schemas.ProfileOut(id=p.id, username=p.username, is_admin=p.is_admin, has_pin=p.pin_hash is not None)
        for p in profiles
    ]
