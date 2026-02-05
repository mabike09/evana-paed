#!/usr/bin/env python3
"""
Backfill 'branch_id' and seed branches for the multi-branch rollout.

What this does (idempotent):
  1) Ensures Branch rows exist for KNY/KYL/WAK.
  2) For legacy data created before multi-branch, sets branch_id = 
<DEFAULT_BRANCH>
     for rows where branch_id IS NULL in these tables:
       - Visit, Invoice, Payment, DentistQueue, BillingQueue, Appointment,
         ItemTxn, DispenseTxn
  3) Ensures ItemStock is initialized for each (branch, item) pair with zeros.

Usage:
  python scripts/backfill_multibranch.py            # dry-run
  python scripts/backfill_multibranch.py --apply    # actually write changes
  python scripts/backfill_multibranch.py --apply --default-branch KYL
"""

import sys
import argparse
from contextlib import contextmanager

from app import app, db
from app import (
    Branch, User, Visit, Invoice, Payment, DentistQueue, BillingQueue,
    Appointment, ItemTxn, DispenseTxn, Item, ItemStock
)

BRANCHES = [
    ("KNY", "Kanyanya"),
    ("KYL", "Kyaliwajjala"),
    ("WAK", "Wakiso"),
]

TARGET_MODELS = [
    ("Visit", Visit),
    ("Invoice", Invoice),
    ("Payment", Payment),
    ("DentistQueue", DentistQueue),
    ("BillingQueue", BillingQueue),
    ("Appointment", Appointment),
    ("ItemTxn", ItemTxn),
    ("DispenseTxn", DispenseTxn),
]

@contextmanager
def appctx():
    with app.app_context():
        yield

def get_or_create_branch(code: str, name: str) -> Branch:
    b = Branch.query.filter_by(code=code).first()
    if not b:
        b = Branch(code=code, name=name, active=True)
        db.session.add(b)
        db.session.flush()  # assign id
    return b

def ensure_branches():
    created = 0
    ids = {}
    for code, name in BRANCHES:
        b = Branch.query.filter_by(code=code).first()
        if not b:
            b = Branch(code=code, name=name, active=True)
            db.session.add(b)
            created += 1
            db.session.flush()
        ids[code] = b.id
    return created, ids

def backfill_branch_ids(default_branch_id: int, dry_run: bool):
    """
    For each target model, set branch_id where NULL to default_branch_id.
    Returns dict: {ModelName: (affected_count, preview_first_ids)}
    """
    results = {}
    for label, Model in TARGET_MODELS:
        # Count NULLs
        null_q = Model.query.filter((getattr(Model, "branch_id") == None))  # 
noqa: E711
        ids = [r.id for r in null_q.limit(25).all()]
        affected = null_q.count()
        results[label] = (affected, ids)

        if not dry_run and affected:
            # Bulk UPDATE for speed
            (db.session.query(Model)
             .filter(getattr(Model, "branch_id") == None)  # noqa: E711
             .update({Model.branch_id: default_branch_id}, 
synchronize_session=False))
    return results

def init_item_stock(all_branch_ids, dry_run: bool):
    """
    Ensure ItemStock exists for every (branch, item). Keep zeros; do not 
override.
    Returns (created_count_estimate).
    """
    items = Item.query.all()
    created = 0
    # Build existing pairs to avoid duplicates
    existing = set(
        (s.branch_id, s.item_id)
        for s in ItemStock.query.with_entities(ItemStock.branch_id, 
ItemStock.item_id).all()
    )
    for bid in all_branch_ids:
        for it in items:
            key = (bid, it.id)
            if key in existing:
                continue
            created += 1
            if not dry_run:
                db.session.add(ItemStock(branch_id=bid, item_id=it.id, 
min_level=0, current_qty=0))
    return created

def maybe_commit(dry_run: bool):
    if dry_run:
        db.session.rollback()
    else:
        db.session.commit()

def main():
    parser = argparse.ArgumentParser(description="Backfill multi-branch data 
safely.")
    parser.add_argument("--apply", action="store_true", help="Apply changes 
(default is dry-run).")
    parser.add_argument("--default-branch", choices=["KNY", "KYL", "WAK"], 
default="KNY",
                        help="Default branch for NULL branch_id rows.")
    args = parser.parse_args()

    dry_run = not args.apply
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"== Multi-branch backfill :: {mode} ==\n")

    with appctx():
        # 1) Ensure branches exist
        created, ids_map = ensure_branches()
        print(f"[Branches] Ensured baseline branches. New created: {created}")
        for c, bid in ids_map.items():
            print(f"  - {c}: id={bid}")
        print()

        # Resolve default branch id
        default_branch_code = args.default_branch.upper()
        default_branch_id = ids_map[default_branch_code]
        print(f"[Backfill] Default branch: {default_branch_code} 
(id={default_branch_id})\n")

        # 2) Backfill branch_id on legacy rows
        results = backfill_branch_ids(default_branch_id, dry_run=dry_run)
        print("[Branch IDs] Rows with NULL branch_id (to be set):")
        total_rows = 0
        for model_name, (affected, sample_ids) in results.items():
            total_rows += affected
            tail = "" if not sample_ids else f" e.g. ids={sample_ids[:10]}"
            print(f"  - {model_name}: {affected}{tail}")
        print(f"  Total rows pending backfill: {total_rows}\n")

        # 3) Ensure ItemStock coverage across all branches
        all_branch_ids = list(ids_map.values())
        stock_created = init_item_stock(all_branch_ids, dry_run=dry_run)
        print(f"[ItemStock] Missing stock rows to create: {stock_created}\n")

        # 4) Optional: set a home_branch_id for non-admin users that currently 
have none.
        #    We *do not* force this, but some teams prefer locking users to a 
home branch.
        add_home = False  # change to True if you want to set 
users.home_branch_id = default for NULLs
        if add_home:
            q = User.query.filter((User.role != "admin") & 
((User.home_branch_id == None)))  # noqa: E711
            n = q.count()
            print(f"[Users] Would set home_branch_id={default_branch_id} for 
{n} user(s) without one.")
            if not dry_run and n:
                q.update({User.home_branch_id: default_branch_id}, 
synchronize_session=False)

        # Commit / rollback
        maybe_commit(dry_run)

        if dry_run:
            print("\nDry-run complete. No changes were written.")
            print("Re-run with --apply to persist the changes.")
        else:
            print("\nApply complete. Changes have been written.")

if __name__ == "__main__":
    main()

