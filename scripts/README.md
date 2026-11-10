# Repair visits returned from the lab

Deploy the lab return fix first. It clears the visit's closure timestamp when completed lab work returns the patient to the doctor. Visits affected before deployment can still have an Open status alongside an old `closed_at` value, which makes the chart read-only.

From the deployed application directory, using the same Python environment and `config.py` as the application, preview an explicitly selected visit:

```bash
python -m scripts.repair_lab_return_visits --visit-id VISIT_ID
```

Replace `VISIT_ID` with the confirmed affected visit's numeric ID. The preview reports only visit IDs and closure timestamps. After checking the preview, apply that selection:

```bash
python -m scripts.repair_lab_return_visits --visit-id VISIT_ID --apply
```

Repeat `--visit-id` to select multiple visits. Every selection must pass validation before any change is committed. The command has no option to apply to all visits.

A selected visit must be Open at the DOCTOR station, have an open **Returned from Lab** doctor queue entry for the same patient, and have completed lab work for that visit. The command refuses visits with pending lab work or conflicting open station queues. A closure timestamp must predate the return queue entry. If a visit is already consistent, a rerun reports that without changing it.

The repair clears only `Visit.closed_at`. It preserves notes, prescriptions, invoices, results, and queue history. Closed historical visits are refused. After applying, reload the selected visit's chart and confirm the prescription and test controls are visible.
