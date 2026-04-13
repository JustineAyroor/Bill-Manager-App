from sqlalchemy import select
from app.db.database import SessionLocal
from app.db.models import User
from app.auth.security import hash_password, verify_password

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


def create_member_user(email: str, password: str) -> str:
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
        )
        db.add(user)
        db.commit()

    return f"Created MEMBER user: {email}"

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
        return user