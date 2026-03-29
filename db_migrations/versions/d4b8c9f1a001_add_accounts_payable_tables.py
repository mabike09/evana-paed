"""add accounts payable tables

Revision ID: d4b8c9f1a001
Revises: b15e699d546c
Create Date: 2026-03-29 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'd4b8c9f1a001'
down_revision = 'b15e699d546c'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'ap_supplier',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('supplier_name', sa.String(length=180), nullable=False),
        sa.Column('contact_person', sa.String(length=150), nullable=True),
        sa.Column('phone_number', sa.String(length=40), nullable=True),
        sa.Column('email', sa.String(length=150), nullable=True),
        sa.Column('physical_address', sa.String(length=255), nullable=True),
        sa.Column('category', sa.String(length=80), nullable=False),
        sa.Column('payment_terms', sa.String(length=40), nullable=False),
        sa.Column('payment_details', sa.String(length=255), nullable=True),
        sa.Column('tax_details', sa.String(length=255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('opening_balance', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('supplier_name', name='uq_ap_supplier_name'),
    )
    op.create_index('ix_ap_supplier_supplier_name', 'ap_supplier', ['supplier_name'])
    op.create_index('ix_ap_supplier_category', 'ap_supplier', ['category'])

    op.create_table(
        'ap_recurring_template',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('supplier_id', sa.Integer(), sa.ForeignKey('ap_supplier.id'), nullable=False),
        sa.Column('template_name', sa.String(length=150), nullable=False),
        sa.Column('expense_category', sa.String(length=80), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('tax_amount', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('frequency', sa.String(length=20), nullable=False, server_default='monthly'),
        sa.Column('due_day', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('reminder_days_before', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('last_generated_on', sa.String(length=10), nullable=True),
        sa.Column('created_by', sa.String(length=150), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )

    op.create_table(
        'ap_bill',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('supplier_id', sa.Integer(), sa.ForeignKey('ap_supplier.id'), nullable=False),
        sa.Column('invoice_number', sa.String(length=80), nullable=False),
        sa.Column('invoice_date', sa.String(length=10), nullable=False),
        sa.Column('due_date', sa.String(length=10), nullable=False),
        sa.Column('expense_category', sa.String(length=80), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('invoice_total', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('tax_amount', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('submitted_by', sa.String(length=150), nullable=False),
        sa.Column('submitted_date', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='unpaid'),
        sa.Column('is_recurring', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('recurring_template_id', sa.Integer(), sa.ForeignKey('ap_recurring_template.id'), nullable=True),
        sa.Column('parent_bill_id', sa.Integer(), sa.ForeignKey('ap_bill.id'), nullable=True),
        sa.Column('created_by', sa.String(length=150), nullable=False),
        sa.Column('updated_by', sa.String(length=150), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('supplier_id', 'invoice_number', name='uq_ap_supplier_invoice'),
    )
    op.create_index('ix_ap_bill_supplier_id', 'ap_bill', ['supplier_id'])
    op.create_index('ix_ap_bill_due_date', 'ap_bill', ['due_date'])

    op.create_table(
        'ap_bill_line',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('bill_id', sa.Integer(), sa.ForeignKey('ap_bill.id'), nullable=False),
        sa.Column('line_description', sa.String(length=255), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('expense_category', sa.String(length=80), nullable=False),
    )

    op.create_table(
        'ap_attachment',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('bill_id', sa.Integer(), sa.ForeignKey('ap_bill.id'), nullable=False),
        sa.Column('document_type', sa.String(length=60), nullable=False),
        sa.Column('file_path', sa.String(length=255), nullable=False),
        sa.Column('uploaded_by', sa.String(length=150), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(), nullable=False),
    )

    op.create_table(
        'ap_payment',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('bill_id', sa.Integer(), sa.ForeignKey('ap_bill.id'), nullable=False),
        sa.Column('payment_date', sa.String(length=10), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('method', sa.String(length=30), nullable=False),
        sa.Column('reference_number', sa.String(length=80), nullable=True),
        sa.Column('paying_account', sa.String(length=120), nullable=True),
        sa.Column('processed_by', sa.String(length=150), nullable=False),
        sa.Column('proof_attachment_path', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )

    op.create_table(
        'ap_audit_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('action', sa.String(length=40), nullable=False),
        sa.Column('entity', sa.String(length=40), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=True),
        sa.Column('actor_username', sa.String(length=150), nullable=False),
        sa.Column('actor_role', sa.String(length=30), nullable=True),
        sa.Column('change_summary', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table('ap_audit_log')
    op.drop_table('ap_payment')
    op.drop_table('ap_attachment')
    op.drop_table('ap_bill_line')
    op.drop_index('ix_ap_bill_due_date', table_name='ap_bill')
    op.drop_index('ix_ap_bill_supplier_id', table_name='ap_bill')
    op.drop_table('ap_bill')
    op.drop_table('ap_recurring_template')
    op.drop_index('ix_ap_supplier_category', table_name='ap_supplier')
    op.drop_index('ix_ap_supplier_supplier_name', table_name='ap_supplier')
    op.drop_table('ap_supplier')
