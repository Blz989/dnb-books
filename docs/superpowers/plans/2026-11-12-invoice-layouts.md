# Invoice layouts and expected delivery date: plan

Spec: `docs/superpowers/specs/2026-11-12-invoice-layouts-design.md`.
PR title: "Invoice layout choice with a Modern layout, and expected delivery on sales orders"

## Global Constraints
- No new dependencies. WeasyPrint and the bundled Inter font only.
- Classic stays the default and its output does not change. Existing invoice PDF tests pass untouched.
- No ledger changes. No posting code is touched except the sales order header fields.
- Screenshots from a throwaway demo database; never the real `data/` database. The owner's real invoice is not committed.

## Review Focus
- Page-break rules in the Modern layout (spec section 2.5), proven by tests on rendered pages.
- The layout choice applies to every invoice on reprint and is Admin-only to change.

## Task 1: Expected delivery date on sales orders
- [x] `SalesOrder.expected_delivery_date` (optional date), migration.
- [x] `create_order` and `update_order` take `expected_delivery_date`; the order form has an optional "Expected delivery" date; the order page shows it. The invoice form does not have the field.
- [x] Tests: `tests/test_invoice_layouts.py` (saved on create, changed and cleared on edit, shown on the order page).
- [x] Screenshots: order form, order page.
- [x] Commit: `Expected delivery date on sales orders`

## Task 2: Invoice layout setting
- [x] `InvoiceLayout` choices (Classic, Modern); `SalesSettings.invoice_layout`, default Classic, migration.
- [x] Settings, Sales: "Invoice layout" select with help text "Applies to every invoice each time it is printed." Admin-only like the rest of the page.
- [x] Tests: default is Classic, an Admin can change it, a post without the field keeps the current choice.
- [x] Screenshot: Settings, Sales.
- [x] Commit: `Invoice layout choice in Sales settings`

## Task 3: Modern invoice layout
- [x] `sales/templates/sales/invoice_pdf_modern.html` and `sales/static/sales/invoice_pdf_modern.css`; `sales/pdf.py` picks the template and stylesheet from the setting.
- [x] Content as spec sections 2.3 and 2.4. Page breaks as spec section 2.5.
- [x] Tests: content, Reference rows with and without a sales order, payments line, one-page invoice has no "Continued" line, long invoice (running header on later pages, column headings on every page with lines, "Continued" on every page but the last, closing block whole on one page, category heading never last on a page), void watermark, Classic unchanged when the setting is Classic.
- [x] Screenshots: Modern PDF with one page, two pages, three pages, and void; Classic for comparison.
- [x] Commit: `Modern invoice layout with page-break rules`

## Task 4: Docs and PR
- [x] README status line and CLAUDE.md pointers.
- [x] Full `uv run pytest -q`.
- [x] PR into `main` with the title above.

## Task 5: Remove payment sections from Modern (follow-up, second PR)
Owner request after the first build. PR title: "Modern invoice: remove How to pay and the How paid heading"
- [x] Remove the "How to pay" section and the "Put invoice number X on your payment." line. Invoice notes stay, under a "Notes" heading, only when present.
- [x] Remove the heading "How paid (check one)" from the sign-off box. The check boxes, Amount paid, signature, printed name, and date stay.
- [x] Tests updated in `tests/test_invoice_layouts.py`.
- [x] Screenshots: Modern PDF, one page and two pages.
- [x] Commit: `Modern invoice: remove How to pay and How paid`, then `Modern invoice: keep the payment check boxes, drop only their heading`

## Task 6: Balance due alignment (follow-up, third PR)
Owner report: the balance due figure in the top bar sat inside a shaded box, inset from the right edge, out of line with the figures below it. PR title: "Modern invoice: line up the balance due figures"
- [x] Top bar and totals: no fill and no right padding on the balance due figures, so they are flush with the Amount column and the Reference values.
- [x] Screenshots: Modern PDF page 1 and last page.
- [x] Commit: `Modern invoice: line up the balance due figures`

## Task 7: Gray balance boxes back, lined up (follow-up, fourth PR)
Owner: the gray boxes from the first build should stay; the alignment should improve. PR title: "Modern invoice: gray balance boxes back, lined up with the Reference column"
- [x] Restore the gray fill and inner padding on both balance due boxes (undoes Task 6).
- [x] Top bar: the balance box is exactly as wide as the Reference column under it.
- [x] Totals: the block is the same width as the Reference column; its rows carry the same side padding as the gray box, so labels and figures form one column. The totals label is "Balance due" (the top bar keeps "(USD)").
- [x] Screenshots: Modern PDF page 1 and last page.
- [x] Commit: `Modern invoice: gray balance boxes back, lined up with the Reference column`
