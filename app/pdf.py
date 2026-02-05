# app/pdf.py
from io import BytesIO
from flask import render_template, Response, current_app
from .models import Invoice, Payment

def _xhtml2pdf_link_callback(uri, rel):
    if uri.startswith('file:'):
        return uri
    if uri.startswith('/static/'):
        return current_app.static_folder + "/" + uri.replace('/static/', '', 1)
    return uri

def invoice_pdf_response(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    html = render_template(
        "invoice_pdf.html",
        invoice=invoice,
        patient=getattr(invoice, "patient", None),
        payments=getattr(invoice, "payments", []),
    )
    try:
        from xhtml2pdf import pisa
    except Exception:
        return Response(html, mimetype="text/html")
    pdf_io = BytesIO()
    result = pisa.CreatePDF(src=html, dest=pdf_io, link_callback=_xhtml2pdf_link_callback, encoding='utf-8')
    if result.err:
        return Response(html, mimetype="text/html")
    return Response(pdf_io.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=invoice_{invoice_id}.pdf"})

def payment_pdf_response(payment_id):
    pay = Payment.query.get_or_404(payment_id)
    inv = getattr(pay, "invoice", None)
    # Prefer patient via invoice; fall back to Payment.patient if your schema has it
    patient = getattr(inv, "patient", None)
    if patient is None and hasattr(pay, "patient"):
        patient = pay.patient

    html = render_template(
        "receipt_print.html",  # ensure this template exists or rename to your actual receipt template
        payment=pay,
        invoice=inv,
        patient=patient,
    )
    try:
        from xhtml2pdf import pisa
    except Exception:
        return Response(html, mimetype="text/html")
    pdf_io = BytesIO()
    result = pisa.CreatePDF(src=html, dest=pdf_io, link_callback=_xhtml2pdf_link_callback, encoding="utf-8")
    if result.err:
        return Response(html, mimetype="text/html")
    return Response(pdf_io.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=receipt_{payment_id}.pdf"})
