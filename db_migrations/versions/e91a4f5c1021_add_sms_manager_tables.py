"""add sms manager tables

Revision ID: e91a4f5c1021
Revises: d4b8c9f1a001
Create Date: 2026-04-03 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'e91a4f5c1021'
down_revision = 'd4b8c9f1a001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'sms_template',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('category', sa.String(length=30), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_by', sa.String(length=150), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_sms_template_name', 'sms_template', ['name'])
    op.create_index('ix_sms_template_category', 'sms_template', ['category'])
    op.create_index('ix_sms_template_is_active', 'sms_template', ['is_active'])
    op.create_index('ix_sms_template_created_at', 'sms_template', ['created_at'])

    op.create_table(
        'sms_dispatch_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('template_id', sa.Integer(), sa.ForeignKey('sms_template.id'), nullable=True),
        sa.Column('campaign_type', sa.String(length=30), nullable=False),
        sa.Column('recipient_phone', sa.String(length=40), nullable=False),
        sa.Column('message_body', sa.Text(), nullable=False),
        sa.Column('provider_response', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_by', sa.String(length=150), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_sms_dispatch_log_template_id', 'sms_dispatch_log', ['template_id'])
    op.create_index('ix_sms_dispatch_log_campaign_type', 'sms_dispatch_log', ['campaign_type'])
    op.create_index('ix_sms_dispatch_log_recipient_phone', 'sms_dispatch_log', ['recipient_phone'])
    op.create_index('ix_sms_dispatch_log_status', 'sms_dispatch_log', ['status'])
    op.create_index('ix_sms_dispatch_log_created_at', 'sms_dispatch_log', ['created_at'])


def downgrade():
    op.drop_index('ix_sms_dispatch_log_created_at', table_name='sms_dispatch_log')
    op.drop_index('ix_sms_dispatch_log_status', table_name='sms_dispatch_log')
    op.drop_index('ix_sms_dispatch_log_recipient_phone', table_name='sms_dispatch_log')
    op.drop_index('ix_sms_dispatch_log_campaign_type', table_name='sms_dispatch_log')
    op.drop_index('ix_sms_dispatch_log_template_id', table_name='sms_dispatch_log')
    op.drop_table('sms_dispatch_log')

    op.drop_index('ix_sms_template_created_at', table_name='sms_template')
    op.drop_index('ix_sms_template_is_active', table_name='sms_template')
    op.drop_index('ix_sms_template_category', table_name='sms_template')
    op.drop_index('ix_sms_template_name', table_name='sms_template')
    op.drop_table('sms_template')
