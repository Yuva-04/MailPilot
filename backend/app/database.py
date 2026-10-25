
import os
from urllib.parse import quote_plus
from dotenv import load_dotenv

load_dotenv()


from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DB_USER = "postgres"
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")

if not DB_PASSWORD:
    raise RuntimeError(
        "POSTGRES_PASSWORD environment variable is not set"
    )
DB_HOST = "localhost"
DB_PORT = "5432"
DB_NAME = "email_campaign_db"

DATABASE_URL = (
    f"postgresql+psycopg://{DB_USER}:"
    f"{quote_plus(DB_PASSWORD)}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
