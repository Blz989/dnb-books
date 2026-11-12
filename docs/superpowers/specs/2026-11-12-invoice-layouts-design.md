# Invoice layouts and expected delivery date

- **Date:** 2026-11-12
- **Status:** Decisions made by the owner (chat, 2026-11-12) on a two-page mockup built from a real invoice; written spec follows them.
- **Parent specs:** `2026-11-03-zoho-style-pdfs-design.md` (the current invoice PDF, kept as the Classic layout).
- **Reference:** the owner's real invoice PDF (never committed). The Modern template in the repo is the visual reference.

## 1. Goal

The owner chooses between defined invoice layouts in Settings. The choice applies to every invoice each time it is printed or reprinted. A second layout, Modern, is added next to the current one.

## 2. Decisions from the owner

1. **Layout choice.** Settings, Sales has an "Invoice layout" choice: Classic (the current boxed layout, the default) and Modern. Invoice PDFs are rendered on request, so the choice applies to old and new invoices alike. Nothing is stored per invoice.
2. **Margins.** Modern uses 0.25 inch on all four sides. The bottom margin also holds one "continued" line on pages that are not the last (0.45 inch in total).
3. **Modern, first page, top to bottom:**
   - Logo, company name and address (phone and email if set). On the right: "Invoice", the invoice number, "Page 1 of N".
   - A bar: Invoice date, Terms, Due date, Balance due.
   - Three columns: Bill to, Ship to, Reference. Reference rows: Invoice no., Invoice date, Terms, Due date, Sales order, Expected delivery. The last two appear only when the invoice came from a sales order (and the order has the date).
   - Lines: #, Item, Packaging, Qty, Unit, Price, Amount. One line per row. Category headings as in Classic (uncategorized first, then A to Z, numbering runs on).
4. **Modern, closing block:**
   - Notes: the invoice notes, under a "Notes" heading, only when the invoice has notes. (Owner, 2026-11-12, after the first build: the "How to pay" section and the "Put invoice number X on your payment." line were removed.)
   - Totals: Total quantity, Invoice total, Payments and credits (only when there are any), Balance due, due date.
   - Customer sign-off box: Customer signature, Printed name, Date, filled in by hand. (Owner, 2026-11-12: the "How paid" check boxes and "Amount paid" line were removed.)
   - No payment instructions, no ACH details, no QR code, no remittance stub, no "Questions" line.
5. **Page breaks (Modern):**
   - A line never splits across pages.
   - A category heading never ends a page; it moves with its first line.
   - The closing block (notes, totals, sign-off) is never split. If it does not fit, the whole block moves to the next page.
   - Every page after the first has a short running header (company, invoice number, customer, date, "Page X of N") and repeats the column headings.
   - Every page except the last ends with "Continued on the next page. Totals and sign-off are on the last page."
6. **Expected delivery date.** A sales order records an optional expected delivery date. It shows on the order form and order page, and prints in Reference on Modern invoices made from that order.
7. **Void invoices** keep the VOID watermark in both layouts.

## 3. Out of scope

- Other documents (statements, purchase orders, checks) keep their layouts.
- Customer PO number, ship date, carrier, tracking.
- A per-customer or per-invoice layout choice.
- The Claude (MCP) sales order tools do not set the expected delivery date yet.
