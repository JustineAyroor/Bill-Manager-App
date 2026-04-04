from app.db.database import engine
from app.db.models import Base

if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    print("✅ Database created: tmobile.db")