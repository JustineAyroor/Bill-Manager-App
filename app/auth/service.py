from dataclasses import dataclass
from datetime import datetime, timedelta
import secrets
import string

from sqlalchemy import select
from app.db.database import SessionLocal
from app.db.models import User
from app.auth.security import hash_password, verify_password


@dataclass
class MemberUserSyncResult:
    ok: bool
    message: str
    user_email: str | None = None
    temp_password: str | None = None
    created: bool = False


@dataclass
class PasswordResetRequestResult:
    ok: bool
    message: str
    user_email: str | None = None
    reset_code: str | None = None
    expires_minutes: int = 30


@dataclass
class AuthenticatedUser:
    id: int
    email: str
    role: str
    is_active: bool
    member_id: int | None

def create_owner_user(email: str, password: str) -> str:
    email = (email or "").strip().lower()
    if not email or not password:
        return "Email and password are required."

    with SessionLocal() as db:
        existing = db.execute(select(User).where(User.email == email)).scalars().first()
        if existing:
            return f"User already exists: {email}"

        user = User(
            email=email,
            password_hash=hash_password(password),
            role="OWNER",
            is_active=True,
        )
        db.add(user)
        db.commit()

    return f"Created owner user: {email}"


def create_member_user(email: str, password: str, member_id: int | None = None) -> str:
    email = (email or "").strip().lower()
    if not email or not password:
        return "Email and password are required."

    with SessionLocal() as db:
        existing = db.execute(select(User).where(User.email == email)).scalars().first()
        if existing:
            return f"User already exists: {email}"

        user = User(
            email=email,
            password_hash=hash_password(password),
            role="MEMBER",
            is_active=True,
            member_id=member_id,
        )
        db.add(user)
        db.commit()

    return f"Created MEMBER user: {email}"


def _generate_temp_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _generate_reset_code(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def ensure_member_user_for_member(email: str, member_id: int) -> MemberUserSyncResult:
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        return MemberUserSyncResult(ok=True, message="No member email provided, so no login was created or linked.")

    with SessionLocal() as db:
        existing = db.execute(select(User).where(User.email == normalized_email)).scalars().first()
        current_link = db.execute(
            select(User).where(User.member_id == member_id, User.role == "MEMBER")
        ).scalars().first()

        if existing:
            role = (existing.role or "").upper()
            if role != "MEMBER":
                return MemberUserSyncResult(
                    ok=False,
                    message=f"Email {normalized_email} already belongs to a non-member account ({role}). "
                            "Use a different email for the member login.",
                    user_email=normalized_email,
                )

            if current_link and current_link.id != existing.id:
                current_link.member_id = None
            existing.member_id = member_id
            db.commit()
            return MemberUserSyncResult(
                ok=True,
                message=f"Linked existing member login: {normalized_email}",
                user_email=normalized_email,
            )

        temp_password = _generate_temp_password()
        if current_link:
            current_link.email = normalized_email
            current_link.password_hash = hash_password(temp_password)
            current_link.member_id = member_id
            db.commit()
            return MemberUserSyncResult(
                ok=True,
                message=f"Updated linked member login email to {normalized_email}",
                user_email=normalized_email,
                temp_password=temp_password,
            )

        user = User(
            email=normalized_email,
            password_hash=hash_password(temp_password),
            role="MEMBER",
            is_active=True,
            member_id=member_id,
        )
        db.add(user)
        db.commit()
        return MemberUserSyncResult(
            ok=True,
            message=f"Created and linked new member login: {normalized_email}",
            user_email=normalized_email,
            temp_password=temp_password,
            created=True,
        )

def authenticate_user(email: str, password: str):
    email = (email or "").strip().lower()
    with SessionLocal() as db:
        user = db.execute(select(User).where(User.email == email)).scalars().first()
        if not user:
            return None
        if not user.is_active:
            return None
        if not verify_password(password, user.password_hash):
            return None
        user.last_login_at = datetime.utcnow()
        user.password_reset_token = None
        user.password_reset_expires_at = None
        db.commit()
        return AuthenticatedUser(
            id=int(user.id),
            email=str(user.email or ""),
            role=str(user.role or ""),
            is_active=bool(user.is_active),
            member_id=int(user.member_id) if user.member_id is not None else None,
        )


def change_user_password(email: str, current_password: str, new_password: str, confirm_password: str) -> str:
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        return "Missing user email."
    if not current_password or not new_password or not confirm_password:
        return "Current password, new password, and confirmation are required."
    if new_password != confirm_password:
        return "New password and confirmation do not match."
    if len(new_password) < 8:
        return "New password must be at least 8 characters."

    with SessionLocal() as db:
        user = db.execute(select(User).where(User.email == normalized_email)).scalars().first()
        if not user:
            return "User not found."
        if not verify_password(current_password, user.password_hash):
            return "Current password is incorrect."
        user.password_hash = hash_password(new_password)
        user.password_reset_token = None
        user.password_reset_expires_at = None
        db.commit()

    return "Password updated."


def request_password_reset(email: str, expires_minutes: int = 30) -> PasswordResetRequestResult:
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        return PasswordResetRequestResult(ok=False, message="Email is required.")

    with SessionLocal() as db:
        user = db.execute(select(User).where(User.email == normalized_email)).scalars().first()
        if not user:
            return PasswordResetRequestResult(ok=False, message="No account found for that email.")
        if not user.is_active:
            return PasswordResetRequestResult(ok=False, message="This account is inactive.")

        reset_code = _generate_reset_code()
        user.password_reset_token = reset_code
        user.password_reset_expires_at = datetime.utcnow() + timedelta(minutes=expires_minutes)
        user.password_reset_sent_at = datetime.utcnow()
        db.commit()

    return PasswordResetRequestResult(
        ok=True,
        message="Password reset code created.",
        user_email=normalized_email,
        reset_code=reset_code,
        expires_minutes=expires_minutes,
    )


def reset_password_with_code(email: str, reset_code: str, new_password: str, confirm_password: str) -> str:
    normalized_email = (email or "").strip().lower()
    code = (reset_code or "").strip().upper()
    if not normalized_email or not code:
        return "Email and reset code are required."
    if not new_password or not confirm_password:
        return "New password and confirmation are required."
    if new_password != confirm_password:
        return "New password and confirmation do not match."
    if len(new_password) < 8:
        return "New password must be at least 8 characters."

    with SessionLocal() as db:
        user = db.execute(select(User).where(User.email == normalized_email)).scalars().first()
        if not user:
            return "User not found."
        if not user.password_reset_token or user.password_reset_token != code:
            return "Invalid reset code."
        if not user.password_reset_expires_at or user.password_reset_expires_at < datetime.utcnow():
            return "Reset code has expired."
        user.password_hash = hash_password(new_password)
        user.password_reset_token = None
        user.password_reset_expires_at = None
        db.commit()

    return "Password reset complete."


def mark_invite_sent(email: str) -> None:
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        return
    with SessionLocal() as db:
        user = db.execute(select(User).where(User.email == normalized_email)).scalars().first()
        if not user:
            return
        user.invite_sent_at = datetime.utcnow()
        db.commit()
