from django.db import migrations

# Database-level backstop for the model guards: even raw SQL cannot delete a
# movement or revision, change a revision, or change a movement other than
# setting its void fields once.
FORWARD = [
    """
    CREATE TRIGGER items_movement_no_delete
    BEFORE DELETE ON items_stockmovement
    BEGIN
        SELECT RAISE(ABORT, 'stock movement is immutable');
    END;
    """,
    """
    CREATE TRIGGER items_movement_no_update
    BEFORE UPDATE ON items_stockmovement
    WHEN OLD.voided = 1
      OR NEW.item_id IS NOT OLD.item_id
      OR NEW.date IS NOT OLD.date
      OR NEW.kind IS NOT OLD.kind
      OR NEW.quantity IS NOT OLD.quantity
      OR NEW.unit_cost IS NOT OLD.unit_cost
      OR NEW.total_cost IS NOT OLD.total_cost
      OR NEW.offset_account_id IS NOT OLD.offset_account_id
      OR NEW.reason IS NOT OLD.reason
      OR NEW.source_type IS NOT OLD.source_type
      OR NEW.source_id IS NOT OLD.source_id
      OR NEW.journal_entry_id IS NOT OLD.journal_entry_id
      OR NEW.count_line_id IS NOT OLD.count_line_id
      OR NEW.created_by_id IS NOT OLD.created_by_id
      OR NEW.created_at IS NOT OLD.created_at
    BEGIN
        SELECT RAISE(ABORT, 'stock movement is immutable');
    END;
    """,
    """
    CREATE TRIGGER items_revision_no_delete
    BEFORE DELETE ON items_costrevision
    BEGIN
        SELECT RAISE(ABORT, 'cost revision is immutable');
    END;
    """,
    """
    CREATE TRIGGER items_revision_no_update
    BEFORE UPDATE ON items_costrevision
    BEGIN
        SELECT RAISE(ABORT, 'cost revision is immutable');
    END;
    """,
]
REVERSE = [
    "DROP TRIGGER items_movement_no_delete;",
    "DROP TRIGGER items_movement_no_update;",
    "DROP TRIGGER items_revision_no_delete;",
    "DROP TRIGGER items_revision_no_update;",
]


class Migration(migrations.Migration):
    dependencies = [("items", "0001_initial")]
    operations = [
        migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE),
    ]
