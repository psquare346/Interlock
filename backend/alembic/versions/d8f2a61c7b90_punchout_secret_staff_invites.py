"""tenant punchout secret + staff invites (invite-only registration)

Revision ID: d8f2a61c7b90
Revises: c2ded11b0196
Create Date: 2026-08-02

Front-door hardening: tenants gain a hashed punchout secret (NULL = punchout
closed for that tenant), and staff_invites carries the single-use, email-bound
tokens that are now the ONLY way to create a staff account. Existing tenants
keep working for logged-in staff; their punchout opens once an admin or the
operator issues a secret (POST /api/tenants/punchout-secret).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd8f2a61c7b90'
down_revision: Union[str, Sequence[str], None] = 'c2ded11b0196'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('punchout_secret_hash', sa.String(length=64), nullable=True))
    op.create_table('staff_invites',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('tenant_id', sa.String(length=40), nullable=False),
        sa.Column('email', sa.String(length=200), nullable=False),
        sa.Column('role', sa.Enum('admin', 'member', name='userrole', native_enum=False), nullable=False),
        sa.Column('privileges', sa.JSON(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('invited_by', sa.String(length=200), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )


def downgrade() -> None:
    op.drop_table('staff_invites')
    op.drop_column('tenants', 'punchout_secret_hash')
