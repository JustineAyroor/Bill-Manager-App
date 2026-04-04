from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Local, zero-install DB:
DATABASE_URL = "sqlite:///./tmobile.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # needed for sqlite + gradio
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

Base = declarative_base()