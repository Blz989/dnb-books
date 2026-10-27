from django.db import migrations

# Database-level backstop for the model guards: even raw SQL cannot change
# or delete a posted entry or its lines. DRAFT -> POSTED and POSTED -> VOID
# (status only) are the only transitions allowed.
FORWARD = [
    """
    CREATE TRIGGER ledger_entry_no_delete
    BEFORE DELETE ON ledger_journalentry
    WHEN OLD.status != 'DRAFT'
    BEGIN
        SELECT RAISE(ABORT, 'posted entry is immutable');
    END;
    """,
    """
    CREATE TRIGGER ledger_entry_no_update
    BEFORE UPDATE ON ledger_journalentry
    WHEN OLD.status = 'VOID' OR (
        OLD.status = 'POSTED' AND (
            NEW.status NOT IN ('POSTED', 'VOID')
            OR NEW.date IS NOT OLD.date
            OR NEW.reference IS NOT OLD.reference
            OR NEW.memo IS NOT OLD.memo
            OR NEW.source_type IS NOT OLD.source_type
            OR NEW.source_id IS NOT OLD.source_id
            OR NEW.reverses_id IS NOT OLD.reverses_id
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'posted entry is immutable');
    END;
    """,
    """
    CREATE TRIGGER ledger_line_no_insert
    BEFORE INSERT ON ledger_journalline
    WHEN (SELECT status FROM ledger_journalentry WHERE id = NEW.entry_id) != 'DRAFT'
    BEGIN
        SELECT RAISE(ABORT, 'posted entry is immutable');
    END;
    """,
    """
    CREATE TRIGGER ledger_line_no_update
    BEFORE UPDATE ON ledger_journalline
    WHEN (SELECT status FROM ledger_journalentry WHERE id = OLD.entry_id) != 'DRAFT'
      OR (SELECT status FROM ledger_journalentry WHERE id = NEW.entry_id) != 'DRAFT'
    BEGIN
        SELECT RAISE(ABORT, 'posted entry is immutable');
    END;
    """,
    """
    CREATE TRIGGER ledger_line_no_delete
    BEFORE DELETE ON ledger_journalline
    WHEN (SELECT status FROM ledger_journalentry WHERE id = OLD.entry_id) != 'DRAFT'
    BEGIN
        SELECT RAISE(ABORT, 'posted entry is immutable');
    END;
    """,
]
REVERSE = [
    "DROP TRIGGER ledger_entry_no_delete;",
    "DROP TRIGGER ledger_entry_no_update;",
    "DROP TRIGGER ledger_line_no_insert;",
    "DROP TRIGGER ledger_line_no_update;",
    "DROP TRIGGER ledger_line_no_delete;",
]


class Migration(migrations.Migration):
    dependencies = [("ledger", "0001_initial")]
    operations = [
        migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE),
    ]
