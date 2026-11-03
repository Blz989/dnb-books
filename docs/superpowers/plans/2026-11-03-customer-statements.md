# Customer statements Implementation Plan

> Executed inline (`superpowers:executing-plans`) with TDD. Spec: `docs/superpowers/specs/2026-11-03-customer-statements-design.md`.
> PR title: `feat: customer statements (page and PDF)`

**Global constraints:** Decimal money; no ledger writes; no new dependencies; plain-language messages; American English; screenshots (demo database only) sent with `SendUserFile`.

**Review focus:** statement closing balance and aging total equal `customer_balance` and the `ar_aging` row for the same date; voids produce reversing events so opening(period N+1) = closing(period N).

## Task 1: `ar_aging` customer filter

**Interface:** `ar_aging(as_of, customer=None)`; with a customer, only that customer's invoices, payments and credit notes are read. Totals row and `ledger_balance` behave as before.

- [x] Test in `tests/test_statements.py`: `ar_aging(d, customer=c).rows` is that customer's row only and equals the same row from the unfiltered report.
- [x] Implement the filter in `sales/reports.py`.
- Commit: `feat: A/R aging can be limited to one customer`

## Task 2: `build_statement`

**Interface:** `sales/statements.py`: `Statement(customer, start, end, opening, rows, closing, aging, open_invoices)`; `StatementRow(date, kind, number, url, description, charge, credit, balance, is_void)`; `build_statement(customer, start, end)`.

- [x] Tests: events and signs for invoice, payment, credit note, refund; void adds a reversing row on `voided_on`; opening + period = closing; closing == `customer_balance(c, end)` == aging row total; chained periods; all customers' closings sum to the A/R account.
- [x] Implement.
- Commit: `feat: customer statement figures`

## Task 3: Statement page and links

- [x] Tests: 200 with figures; 404 unknown customer; login required; From after To shows "From must be on or before To."; defaults to last 90 days; links on the customer list and customer page.
- [x] View `sales/views/statements.py`, route, template `sales/statement.html`, links in `contacts` templates.
- Commit: `feat: customer statement page`

## Task 4: Statement PDF

- [x] Tests: HTML has company, customer, opening, closing, aging; PDF bytes start with `%PDF`; view returns inline PDF; many rows paginate with a repeating header.
- [x] `statement_pdf`, template, CSS, view.
- Commit: `feat: customer statement PDF`

## Task 5: Docs and screenshots

- [x] README status/feature line; render screenshots from a throwaway demo database; send them; list in PR.
- Commit: `docs: README mentions customer statements`
