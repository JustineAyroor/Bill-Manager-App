"""Bootstrap DB: create missing tables, then apply Alembic migrations.

Use Alembic for schema changes after the initial layout exists:

  uv run alembic revision --autogenerate -m "short description"
  uv run alembic upgrade head

`create_all()` only creates whole tables that are missing; Alembic handles
new columns, renames, and constraints on existing tables.
"""

from alembic import command
from alembic.config import Config

from app.db.database import engine
from app.db.models import Base


def upgrade_migrations() -> None:
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    upgrade_migrations()
    print("Database ready:", engine.url)
