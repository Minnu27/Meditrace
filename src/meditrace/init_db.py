"""Create missing tables without dropping existing data: python -m src.meditrace.init_db."""

from .database import create_schema

if __name__ == "__main__":
    create_schema()
    print(
        "Missing Meditrace tables created. Existing table definitions/data are unchanged."
    )
