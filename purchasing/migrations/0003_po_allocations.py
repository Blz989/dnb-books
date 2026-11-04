"""A drop-ship PO line used to point at one sales order line. It now has allocations, so one PO line can serve
several sales orders. Each old link becomes one allocation of the PO line's full quantity."""
import django.db.models.deletion
from django.db import migrations, models

SHARED = "A purchase order line is shared by several sales orders. Undo the sharing first."


def forward(apps, schema_editor):
    PurchaseOrder = apps.get_model("purchasing", "PurchaseOrder")
    PurchaseOrderLine = apps.get_model("purchasing", "PurchaseOrderLine")
    Allocation = apps.get_model("purchasing", "PurchaseOrderAllocation")
    for line in PurchaseOrderLine.objects.filter(so_line__isnull=False).order_by("id"):
        Allocation.objects.create(po_line_id=line.pk, so_line_id=line.so_line_id, quantity=line.quantity)
    # A PO edited before this change lost its line links but kept its sales order. Keep that on the screen.
    for po in PurchaseOrder.objects.filter(sales_order__isnull=False).select_related("sales_order").order_by("id"):
        if not Allocation.objects.filter(po_line__order=po, so_line__order_id=po.sales_order_id).exists():
            note = f"Drop ship for {po.sales_order.number}."
            po.notes = f"{po.notes}\n{note}" if po.notes else note
            po.save(update_fields=["notes"])


def backward(apps, schema_editor):
    PurchaseOrder = apps.get_model("purchasing", "PurchaseOrder")
    PurchaseOrderLine = apps.get_model("purchasing", "PurchaseOrderLine")
    Allocation = apps.get_model("purchasing", "PurchaseOrderAllocation")
    for line in PurchaseOrderLine.objects.filter(allocations__isnull=False).distinct().order_by("id"):
        allocations = list(Allocation.objects.filter(po_line=line))
        if len(allocations) > 1:
            raise RuntimeError(SHARED)
        line.so_line_id = allocations[0].so_line_id
        line.save(update_fields=["so_line"])
    for po in PurchaseOrder.objects.order_by("id"):
        orders = set(Allocation.objects.filter(po_line__order=po).values_list("so_line__order_id", flat=True))
        if len(orders) == 1:
            po.sales_order_id = orders.pop()
            po.save(update_fields=["sales_order"])


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0002_seed_thresholds"),
        ("sales", "0006_clearing_other_entries"),
    ]

    operations = [
        migrations.CreateModel(
            name="PurchaseOrderAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.PositiveIntegerField()),
                ("po_line", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="allocations",
                                              to="purchasing.purchaseorderline")),
                ("so_line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                              related_name="po_allocations", to="sales.salesorderline")),
            ],
            options={
                "ordering": ["so_line__order__date", "so_line__order_id", "so_line_id", "id"],
                "constraints": [models.UniqueConstraint(fields=("po_line", "so_line"), name="po_allocation_unique")],
            },
        ),
        migrations.RunPython(forward, backward),
        migrations.RemoveField(model_name="purchaseorderline", name="so_line"),
        migrations.RemoveField(model_name="purchaseorder", name="sales_order"),
    ]
