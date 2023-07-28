"""user table updates

Revision ID: d86c72388ae3
Revises: 47fa5e8afe44
Create Date: 2023-07-28 11:30:17.906652

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd86c72388ae3'
down_revision = '47fa5e8afe44'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('email', sa.String(length=80), nullable=False))
        batch_op.drop_constraint('user_username_key', type_='unique')
        batch_op.create_unique_constraint(None, ['email'])
        batch_op.drop_column('username')

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('username', sa.VARCHAR(length=80), autoincrement=False, nullable=False))
        batch_op.drop_constraint(None, type_='unique')
        batch_op.create_unique_constraint('user_username_key', ['username'])
        batch_op.drop_column('email')

    # ### end Alembic commands ###
