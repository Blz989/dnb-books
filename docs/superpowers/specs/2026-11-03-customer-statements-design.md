# Customer statements

- **Date:** 2026-11-03
- **Status:** Draft for review
- **Parent spec:** `docs/superpowers/specs/2026-10-27-zoho-replacement-design.md`. Customer statements were listed as out of scope in `docs/superpowers/specs/2026-10-27-phase-3-sales-design.md` (section 2). This feature is new; where they differ, this document wins. Emailing stays out of scope.
- **Builds on:** Phase 3 (sales), the A/R aging report, the invoice PDF.
- **Visual reference:** `docs/design/items-mockup.html` (screen) and `docs/design/invoice-mockup.html` (PDF).

## 1. Goal

Give each customer a statement: what they were billed, what they paid or were credited, and what they owe now. The owner opens it from the Customers screen and downloads a PDF to send by hand.

### Success criteria (done when)

1. From Customers (the list and each customer's page) a Statement page opens for that customer, with a PDF download.
2. The closing balance equals the customer's row in the A/R aging report for the same date, to the cent. The aging summary on the statement equals that row too.
3. A voided document disappears from the balance on its void date and shows as its own reversing line, so the opening balance of any period equals the closing balance of the period before it.
4. Tests pass, and screenshots of the statement page (normal, empty, error) and the rendered PDF have been sent to the owner.

## 2. Decisions from the owner

| Topic | Decision |
|---|---|
| Contents | Opening ("balance forward"), every invoice, payment, credit note, and refund in the date range with a running balance, closing balance, and an aging summary. |
| Who | One customer at a time. No batch of all customers. |
| Dates | From and To. Defaults: To is today, From is 90 days earlier. Both editable. |
| PDF | Matches the invoice PDF: logo, company details, fonts, colors. "STATEMENT" replaces "INVOICE". No bank details or remittance stub. |
| Emailing | Out of scope. |

## 3. How the numbers are made

Same source as the A/R aging report: the sales documents and their dated applications (`sales/balances.py`), not new tables. The ledger tie-out stays where it already is: total customer balances against the A/R account (`ar_check`). No new ledger writes.

Every document is turned into dated **events** on the customer's account:

| Document | Event | Effect on balance |
|---|---|---|
| Invoice | on `date`, total of lines | + |
| Payment | on `date`, `amount` | − |
| Credit note | on `date`, total of lines | − |
| Refund | on `date`, `amount` | + (money returned to the customer) |
| Any voided document | a second event on `voided_on` with the opposite sign, labelled "Void of …" | reverses |

Applying a payment or credit to an invoice moves nothing on the statement: the payment already reduced the balance when it was received. That is why the closing balance (invoices − payments − credits + refunds) equals the aging total, which is open invoices less unapplied payments and credits.

- **Opening balance** = sum of events dated before From.
- **Running balance** after each event in the range = opening + events so far. Events on the same day sort invoices, credit notes, payments, refunds, then number.
- **Closing balance** = opening + all events through To. Must equal `customer_balance(customer, To)`.
- **Aging summary** = the customer's A/R aging row as of To: Current, 1 to 30, 31 to 60, 61 to 90, Over 90, Unapplied credits, Total. Buckets use days past due, as the report does.
- Open invoices as of To are listed under the aging summary on the page and the PDF (number, date, due date, balance), so the customer sees what to pay.

If From is after To, the page shows "From must be on or before To." and no statement. A customer with no activity and a zero balance shows an empty-state message and still produces a PDF stating zero.

## 4. Screens and routes

| Route | What |
|---|---|
| `/sales/customers/<id>/statement/?start=YYYY-MM-DD&end=YYYY-MM-DD` | Statement page: date form, Download PDF button, balance forward, activity table, closing balance, aging summary, open invoices. |
| `/sales/customers/<id>/statement/pdf/?start=&end=` | PDF, inline, filename `Statement-<customer>-<end>.pdf`. |

- Customers list: a "Statement" link on each row. Customer page: a "Statement" button in the header actions.
- All signed-in roles can view, as with the aging report. Document numbers link to their pages.

## 5. Code

- `sales/statements.py`: `build_statement(customer, start, end)` returns a dataclass (opening, rows with running balance, closing, aging row, open invoices). Pure reads.
- `sales/reports.py`: `ar_aging(as_of, customer=None)` gains an optional customer filter so the statement reuses the report's own bucket logic instead of copying it.
- `sales/pdf.py`: `statement_pdf(statement)`; template `sales/statement_pdf.html`; styles extend the invoice CSS in `sales/static/sales/statement_pdf.css`.
- `sales/views/statements.py`, `sales/templates/sales/statement.html`, URLs in `sales/urls.py`.

## 6. Testing

- Opening + period events = closing, for several ranges, with voids inside and outside the range.
- Closing and aging total equal `customer_balance` and the `ar_aging` row for the customer, on a scripted set: invoice, partial payment, unapplied payment, credit note applied and unapplied, refund, a voided invoice, a payment voided later.
- Statements of all customers, summed, equal the A/R account balance.
- Views: 200 for a customer, 404 for an unknown id, error message for From after To, login required, PDF starts with `%PDF`.

## 7. Out of scope

Emailing, batch statements, finance charges, statement numbering or history, per-invoice line detail on the statement.
