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
    Combined login AND first-time PIN setup. A profile's pin_hash starts
    out null (set that way by the profiles migration, or whenever an
    admin creates a new profile later) — the FIRST successful call here
    for that profile sets it, rather than requiring a separate "register"
    step. Every call after that verifies against what got set.
    """
    if not PIN_RE.match(req.pin.strip()):
        raise HTTPException(400, "PIN must be exactly 6 digits.")
    pin = req.pin.strip()

    profile = db.query(models.Profile).filter(models.Profile.username == req.username).first()
    if not profile:
        raise HTTPException(404, "No profile with that username.")

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
    Reusable dependency for later phases: pulls the token out of an
    `Authorization: Bearer <token>` header, looks up the session, returns
    the profile it belongs to. Not used by any endpoint YET — phase 3 is
    where routers actually start requiring this.
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


def get_current_profile_or_default(
    authorization: str = Header(default=None),
    db: DbSession = Depends(get_db),
) -> models.Profile:
    """
    Phase-3 safety net: behaves exactly like get_current_profile when a
    valid token is present, but falls back to the original starter
    profile (whoever has the lowest id — "Mata", from the profiles
    migration) instead of raising 401 when there's no token at all.

    This is what lets enforcement get turned on router-by-router without
    ever taking the live, not-yet-updated frontend down — every request
    it currently makes has no Authorization header, so it keeps landing
    on the same profile that already owns all the existing data. Once
    the frontend actually sends real tokens (phase 4) and that's been
    confirmed working, this fallback gets removed (phase 5) so a request
    with no valid token is genuinely rejected instead of silently
    allowed through.

    A malformed/expired token is still a real error, not a silent
    fallback — this only relaxes the "nothing sent at all" case.
    """
    if not authorization:
        default_profile = db.query(models.Profile).order_by(models.Profile.id).first()
        if not default_profile:
            raise HTTPException(401, "No profile exists yet.")
        return default_profile
    return get_current_profile(authorization=authorization, db=db)


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
