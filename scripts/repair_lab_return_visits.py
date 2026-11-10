"""Preview or repair explicitly selected visits left read-only after lab return.

Run from the application directory with its production configuration:
    python -m scripts.repair_lab_return_visits --visit-id VISIT_ID
    python -m scripts.repair_lab_return_visits --visit-id VISIT_ID --apply
"""

import argparse
import sys


class RepairError(ValueError):
    """The selected visit does not have sufficient evidence of an active return."""


def repair_visits(visit_ids, *, apply=False):
    """Clear only stale closure timestamps for validated, explicitly named visits."""
    from app.extensions import db
    from app.models import BillingQueue, LabOrder, Visit

    ids = sorted(set(visit_ids))
    if not ids or any(not isinstance(visit_id, int) or visit_id <= 0 for visit_id in ids):
        raise RepairError("Provide at least one positive visit ID.")

    visits = Visit.query.filter(Visit.id.in_(ids)).order_by(Visit.id).with_for_update().all()
    by_id = {visit.id: visit for visit in visits}
    repairs = []
    for visit_id in ids:
        visit = by_id.get(visit_id)
        if visit is None:
            raise RepairError(f"Visit #{visit_id} does not exist.")
        if (visit.status or "").strip().lower() != "open":
            raise RepairError(f"Visit #{visit_id} is not Open.")
        if (visit.current_station or "").strip().upper() != "DOCTOR":
            raise RepairError(f"Visit #{visit_id} is not at the DOCTOR station.")

        queues = BillingQueue.query.filter_by(visit_id=visit.id, status="Open").all()
        returned = [
            queue for queue in queues
            if queue.patient_id == visit.patient_id
            and (queue.kind or "").strip().upper() == "DOCTOR"
            and (queue.description or "").strip().lower() == "returned from lab"
        ]
        if not returned:
            raise RepairError(f"Visit #{visit_id} has no active Returned from Lab doctor entry.")
        if any((queue.kind or "").strip().upper() != "DOCTOR" for queue in queues):
            raise RepairError(f"Visit #{visit_id} has a conflicting open queue entry.")

        orders = LabOrder.query.filter_by(visit_id=visit.id, patient_id=visit.patient_id)
        if orders.filter_by(status="Completed").first() is None:
            raise RepairError(f"Visit #{visit_id} has no completed lab order.")
        if orders.filter(LabOrder.status.in_(["Pending", "PendingPayment"])).first():
            raise RepairError(f"Visit #{visit_id} still has pending lab work.")
        if visit.closed_at is not None and not any(
            queue.added_at is not None and queue.added_at > visit.closed_at for queue in returned
        ):
            raise RepairError(f"Visit #{visit_id} was not closed before its lab return.")

        repairs.append((visit, visit.closed_at))

    # Do not change any row until every selected visit has passed validation.
    if apply:
        try:
            for visit, closed_at in repairs:
                if closed_at is not None:
                    visit.closed_at = None
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

    return [
        {"visit_id": visit.id, "previous_closed_at": closed_at,
         "action": "already consistent" if closed_at is None else ("cleared" if apply else "would clear")}
        for visit, closed_at in repairs
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--visit-id", action="append", type=int, required=True,
                        help="Visit to inspect; repeat for multiple explicitly selected visits.")
    parser.add_argument("--apply", action="store_true", help="Commit repairs (default: preview only).")
    args = parser.parse_args(argv)

    from app import create_app

    with create_app().app_context():
        try:
            repairs = repair_visits(args.visit_id, apply=args.apply)
        except RepairError as error:
            print(f"Refused: {error} No visits changed.", file=sys.stderr)
            return 1
        for repair in repairs:
            print(f"Visit #{repair['visit_id']}: {repair['action']}"
                  f" (previous closed_at={repair['previous_closed_at']}).")
        if not args.apply:
            print("Preview only; no visits changed. Add --apply to commit these repairs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
