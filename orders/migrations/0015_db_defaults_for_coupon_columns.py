"""
Hotfix: the coupon migration from `dev` (orders 0015_order_coupon_...) was
applied to the production database, adding NOT NULL columns
(`orders_order.coupon_code`, `*.discount_amount`) that only carry
Python-side defaults. Code on `main` doesn't know about those fields, so
Django omits them from INSERTs and Postgres rejects the row.

Give those columns database-level defaults when they exist. No-op on
databases that don't have them (fresh installs, SQLite test DBs).
"""
from django.db import migrations

COLUMN_DEFAULTS = [
    ('orders_order', 'coupon_code', "''"),
    ('orders_order', 'discount_amount', '0'),
    ('orders_suborder', 'discount_amount', '0'),
    ('orders_orderitem', 'discount_amount', '0'),
]


def _existing_columns(connection, table):
    with connection.cursor() as cursor:
        if table not in connection.introspection.table_names(cursor):
            return set()
        return {c.name for c in connection.introspection.get_table_description(cursor, table)}


def set_db_defaults(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'postgresql':
        return
    for table, column, default in COLUMN_DEFAULTS:
        if column in _existing_columns(connection, table):
            schema_editor.execute(
                f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET DEFAULT {default}'
            )


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0014_sellersettlement_status'),
    ]

    operations = [
        migrations.RunPython(set_db_defaults, migrations.RunPython.noop),
    ]
