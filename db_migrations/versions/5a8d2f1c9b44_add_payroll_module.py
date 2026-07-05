"""add payroll module

Revision ID: 5a8d2f1c9b44
Revises: a2f4c9d8e012, 9f2c1a4d7e11
Create Date: 2026-07-05
"""
from alembic import op
import sqlalchemy as sa

revision = "5a8d2f1c9b44"
down_revision = ("a2f4c9d8e012", "9f2c1a4d7e11")
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('staff_member', sa.Column('id', sa.Integer(), primary_key=True), sa.Column('staff_id', sa.String(30), unique=True), sa.Column('first_name', sa.String(80), nullable=False), sa.Column('last_name', sa.String(80), nullable=False), sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), unique=True), sa.Column('role', sa.String(80), nullable=False), sa.Column('employment_type', sa.String(30), nullable=False), sa.Column('salary_type', sa.String(30), nullable=False), sa.Column('basic_salary', sa.Numeric(12,2), nullable=False), sa.Column('bank_details', sa.String(255)), sa.Column('mobile_money_details', sa.String(120)), sa.Column('nssf_number', sa.String(80)), sa.Column('tin', sa.String(80)), sa.Column('start_date', sa.String(10), nullable=False), sa.Column('contract_status', sa.String(30), nullable=False), sa.Column('department', sa.String(80), nullable=False), sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('updated_at', sa.DateTime(), nullable=False))
    op.create_table('payroll_period', sa.Column('id', sa.Integer(), primary_key=True), sa.Column('name', sa.String(80), nullable=False, unique=True), sa.Column('period_month', sa.String(7), nullable=False, unique=True), sa.Column('status', sa.String(30), nullable=False), sa.Column('revenue', sa.Numeric(12,2), nullable=False), sa.Column('created_by', sa.String(150), nullable=False), sa.Column('approved_by', sa.String(150)), sa.Column('approved_at', sa.DateTime()), sa.Column('paid_by', sa.String(150)), sa.Column('paid_at', sa.DateTime()), sa.Column('locked_by', sa.String(150)), sa.Column('locked_at', sa.DateTime()), sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_table('payroll_component', sa.Column('id', sa.Integer(), primary_key=True), sa.Column('staff_id', sa.Integer(), sa.ForeignKey('staff_member.id'), nullable=False), sa.Column('name', sa.String(80), nullable=False), sa.Column('component_type', sa.String(20), nullable=False), sa.Column('amount', sa.Numeric(12,2), nullable=False), sa.Column('is_active', sa.Boolean(), nullable=False))
    op.create_table('staff_loan', sa.Column('id', sa.Integer(), primary_key=True), sa.Column('staff_id', sa.Integer(), sa.ForeignKey('staff_member.id'), nullable=False), sa.Column('loan_type', sa.String(30), nullable=False), sa.Column('principal_amount', sa.Numeric(12,2), nullable=False), sa.Column('monthly_deduction', sa.Numeric(12,2), nullable=False), sa.Column('outstanding_balance', sa.Numeric(12,2), nullable=False), sa.Column('approved_by', sa.String(150), nullable=False), sa.Column('approval_date', sa.String(10), nullable=False), sa.Column('notes', sa.Text()), sa.Column('is_active', sa.Boolean(), nullable=False))
    op.create_table('payroll_line', sa.Column('id', sa.Integer(), primary_key=True), sa.Column('period_id', sa.Integer(), sa.ForeignKey('payroll_period.id'), nullable=False), sa.Column('staff_id', sa.Integer(), sa.ForeignKey('staff_member.id'), nullable=False), sa.Column('attendance_days', sa.Numeric(8,2), nullable=False), sa.Column('attendance_hours', sa.Numeric(8,2), nullable=False), sa.Column('basic_pay', sa.Numeric(12,2), nullable=False), sa.Column('allowances', sa.Numeric(12,2), nullable=False), sa.Column('overtime_pay', sa.Numeric(12,2), nullable=False), sa.Column('locum_pay', sa.Numeric(12,2), nullable=False), sa.Column('deductions', sa.Numeric(12,2), nullable=False), sa.Column('loan_deductions', sa.Numeric(12,2), nullable=False), sa.Column('nssf', sa.Numeric(12,2), nullable=False), sa.Column('paye', sa.Numeric(12,2), nullable=False), sa.Column('gross_pay', sa.Numeric(12,2), nullable=False), sa.Column('net_pay', sa.Numeric(12,2), nullable=False))
    op.create_table('payroll_audit_log', sa.Column('id', sa.Integer(), primary_key=True), sa.Column('period_id', sa.Integer(), sa.ForeignKey('payroll_period.id')), sa.Column('action', sa.String(40), nullable=False), sa.Column('actor_username', sa.String(150), nullable=False), sa.Column('actor_role', sa.String(30)), sa.Column('change_summary', sa.Text()), sa.Column('created_at', sa.DateTime(), nullable=False))


def downgrade():
    for table in ['payroll_audit_log', 'payroll_line', 'staff_loan', 'payroll_component', 'payroll_period', 'staff_member']:
        op.drop_table(table)
