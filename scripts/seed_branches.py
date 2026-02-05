# scripts/seed_branches.py
"""
Seed initial Branch records (idempotent).
- Auto-filters defaults to only valid Branch columns, so it won't crash
  if some suggested fields (e.g., 'phone') don't exist in your model.
"""

from datetime import datetime

try:
    from app import app, db, Branch  # noqa
except ImportError:
    from app import app, db  # noqa
    from models import Branch  # noqa


def branch_columns():
    """Return a set of column names actually present on Branch."""
    return {c.name for c in Branch.__table__.columns}


def filter_defaults_for_branch(defaults, valid_cols):
    """Keep only keys that exist on Branch; drop the rest."""
    return {k: v for k, v in (defaults or {}).items() if k in valid_cols}


def get_or_create(session, model, unique_kwargs, create_kwargs=None):
    """
    Find by unique fields (e.g., code=...). If missing, create with create_kwargs.
    """
    instance = session.query(model).filter_by(**unique_kwargs).first()
    if instance:
        return instance, False
    instance = model(**{**unique_kwargs, **(create_kwargs or {})})
    session.add(instance)
    return instance, True


def main():
    with app.app_context():
        cols = branch_columns()
        # Helpful print so you can see what your model actually has
        print("Branch columns detected:", sorted(cols))

        # ---- Define seeds (use only keys that exist on YOUR model) ----
        # Keep 'code' (assumed unique) + whatever other columns your model has.
        seeds = [
            {
                "code": "KNY",
                "defaults": {
                    "name": "Kanyanya",
                    "email": "hq@bambidental.com",
                    "address": "Kampala, Uganda",
                    "is_active": True,
                    "created_at": datetime.utcnow(),
                },
            },
            {
                "code": "KIREKA",
                "defaults": {
                    "name": "Bambi Dental — Kireka",
                    "email": "kireka@bambidental.com",
                    "address": "Kireka, Kampala",
                    "is_active": True,
                    "created_at": datetime.utcnow(),
                },
            },
            {
                "code": "KYALI",
                "defaults": {
                    "name": "Bambi Dental — Kyaliwajjala",
                    "email": "kyali@bambidental.com",
                    "address": "Kyaliwajjala, Kampala",
                    "is_active": True,
                    "created_at": datetime.utcnow(),
                },
            },
        ]

        created = 0
        for s in seeds:
            code = s["code"]
            # Only keep defaults that actually exist on Branch:
            clean_defaults = filter_defaults_for_branch(s.get("defaults", {}), cols)

            # Also drop 'created_at' if Branch doesn't have it:
            if "created_at" in clean_defaults and "created_at" not in cols:
                clean_defaults.pop("created_at", None)

            # If your model uses a different boolean, map it here:
            if "is_active" in clean_defaults and "is_active" not in cols:
                # Try common alternatives:
                if "active" in cols:
                    clean_defaults["active"] = clean_defaults.pop("is_active")
                else:
                    clean_defaults.pop("is_active", None)

            _, was_created = get_or_create(
                db.session,
                Branch,
                unique_kwargs={"code": code},
                create_kwargs=clean_defaults,
            )
            if was_created:
                created += 1

        db.session.commit()
        print(f"Seed complete. Created {created} new branch(es).")


if __name__ == "__main__":
    main()
