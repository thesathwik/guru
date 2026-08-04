"""Sign-in, backed by Google Identity Platform.

The browser signs in against Identity Platform and sends the resulting ID
token as a bearer token; this verifies it and maps it to a row in `users`.
No password ever reaches this application - Identity Platform stores and
checks them, along with reset flows and rate limiting - so the whole class
of password-handling bugs simply does not exist here.

Verification uses google-auth, which the app already depends on for
Vertex, rather than pulling in the Firebase Admin SDK for one function.
"""
import os
from datetime import datetime

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from . import models
from .database import get_db

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")

# Comma-separated emails allowed to manage the shared library. Everyone
# else can only add material for themselves.
ADMIN_EMAILS = {
    email.strip().lower()
    for email in os.environ.get("ADMIN_EMAILS", "").split(",")
    if email.strip()
}

# Lets the app run without sign-in for local development and for the
# docker-compose setup, where there is no Identity Platform to verify
# against. Never leave this on for a deployment that is reachable
# publicly: it hands every request the same implicit identity.
AUTH_DISABLED = os.environ.get("AUTH_DISABLED", "0") not in ("0", "false", "False")

_request_adapter = None


class AuthError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status_code=401, detail=detail)


def _verify(token: str) -> dict:
    """Returns the token's claims, or raises if it is not a valid,
    unexpired token issued by this project."""
    global _request_adapter

    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    if _request_adapter is None:
        _request_adapter = google_requests.Request()

    try:
        claims = google_id_token.verify_firebase_token(
            token, _request_adapter, audience=PROJECT_ID
        )
    except Exception as exc:  # noqa: BLE001 - any failure here means "not signed in"
        raise AuthError(f"Sign-in token rejected: {exc}")

    if not claims:
        raise AuthError("Sign-in token rejected")
    # verify_firebase_token checks the audience, but not that the issuer
    # is this project's - a token minted by a different Identity Platform
    # project would otherwise be accepted.
    if claims.get("iss") != f"https://securetoken.google.com/{PROJECT_ID}":
        raise AuthError("Sign-in token was issued for a different project")
    return claims


def _user_from_claims(db: Session, claims: dict) -> models.User:
    uid = claims.get("user_id") or claims.get("sub")
    if not uid:
        raise AuthError("Sign-in token carries no user id")

    email = (claims.get("email") or "").lower() or None
    user = db.query(models.User).filter_by(auth_uid=uid).first()

    if user is None:
        user = models.User(
            auth_uid=uid,
            email=email,
            display_name=claims.get("name") or claims.get("email"),
            is_admin=bool(email and email in ADMIN_EMAILS),
        )
        db.add(user)
    else:
        # Keep these current: a student can change their display name, and
        # admin rights follow the configured list rather than being frozen
        # at whatever it said when they first signed in.
        user.email = email or user.email
        user.display_name = claims.get("name") or user.display_name
        user.is_admin = bool(email and email in ADMIN_EMAILS)

    user.last_seen_at = datetime.utcnow()
    db.commit()
    _claim_invitations(db, user)
    db.refresh(user)
    return user


def _claim_invitations(db: Session, user: models.User) -> None:
    """Attaches this account to any class it was invited to by email.

    A teacher builds a roster before the students have signed up, so the
    invitation is stored against an address. This is where it becomes a
    membership - which also means a student who registers later, or who is
    added after they registered, ends up in the class either way.
    """
    if not user.email:
        return

    pending = (
        db.query(models.ClassMember)
        .filter(
            models.ClassMember.email == user.email,
            models.ClassMember.user_id.is_(None),
        )
        .all()
    )
    if not pending:
        return

    for member in pending:
        member.user_id = user.id
        member.joined_at = datetime.utcnow()
    db.commit()


def _development_user(db: Session) -> models.User:
    user = db.query(models.User).filter_by(auth_uid="dev").first()
    if user is None:
        user = models.User(
            auth_uid="dev", email="dev@localhost", display_name="Developer",
            is_admin=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> models.User:
    """FastAPI dependency: the signed-in user, or 401."""
    if AUTH_DISABLED:
        return _development_user(db)

    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError("Sign in to continue")

    return _user_from_claims(db, _verify(authorization.split(" ", 1)[1].strip()))


def require_admin(user: models.User = Depends(current_user)) -> models.User:
    if not user.is_admin:
        raise HTTPException(403, "Only an administrator can change the shared library")
    return user


def require_teacher(user: models.User = Depends(current_user)) -> models.User:
    if not (user.is_teacher or user.is_admin):
        raise HTTPException(403, "Only a teacher can do that")
    return user
