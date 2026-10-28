# Order profitability and handling

- **Date:** 2026-10-28
- **Status:** Draft for review
- **Parent spec:** `docs/superpowers/specs/2026-10-27-zoho-replacement-design.md`. This feature is new; the parent does not cover it. Where they differ, this document wins.
- **Builds on:** Phases 1 to 5a (sales orders, invoices, credit notes, stock costing, drop-ship bills, vendor bills and credits, month close checks).
- **Order of work:** built before Phase 5b (Zoho migration).
- **Visual reference:** `docs/design/items-mockup.html` ("Ledger paper"; all screens match it).

## 1. Goal

Show the profit of every order, and pay a separate handling company 55% of that profit.

- Each order's profit is its sales less the cost of what was shipped on it.
- Twice a month, on the 15th and the last day, the owner reviews what is owed and creates one bill to the handling company for 55% of each order's profit not yet billed. The bill is paid through Pay bills like any other.

### Success criteria (done when)

1. A scripted month passes, with every ledger check (inventory, AR, Undeposited, A/P) at 0.00 throughout:
   1. Ship and invoice a stock order, a drop-ship order, and a mixed order shipped in two invoices. Invoice one drop-ship line before its vendor bill is entered.
   2. Create the handling bill for the 1st to 15th. Check each order's row by hand.
   3. Enter the late drop-ship bill, apply a customer credit to one invoice, and edit another invoice.
   4. Create the handling bill for the 16th to the end of the month. Its rows show the corrections.
   5. Pay both handling bills. Close the month.
2. Total handling billed (bills less vendor credits) equals the balance of the Handling expense account in the ledger.
3. Screenshots of every new screen and the statement PDF, light and dark, have been sent to the owner.

## 2. Decisions from the owner

| Topic | Decision |
|---|---|
| Who is paid | A separate company. It is a vendor; handling is an expense owed to it. |
| Profit | Sales less product cost: stock cost (FIFO) plus drop-ship bill cost. Freight and other costs are not subtracted. |
| Rate | 55%. One rate in settings with an effective date. A change applies to orders dated on or after that date. |
| Losing orders | Handling is never below zero for an order. The handling company never owes DNB for a loss. |
| Billing | Twice a month: 1st to 15th, and 16th to the last day. The owner clicks Create bill; nothing is booked automatically. |
| How it is booked | No per-invoice entries. Each handling bill carries 55% of each order's profit to date, less what earlier handling bills already carried for that order. |
| Direct invoices | An invoice with no sales order is its own order. |
| Credit notes | A credit reduces the profit of the order whose invoice it is applied to. If the application is removed or moved, the reduction moves with it. |
| Drop-ship bill not yet entered | Bill handling on the profit as it stands (that line's cost counts as 0.00). The next handling bill corrects it. |

### Out of scope

- Per-invoice handling entries or accruals.
- Freight, card fees, or other costs in profit.
- Rates by customer or by order.
- Creating handling bills automatically.
- Emailing the statement.
- Payroll or commissions.

## 3. Modules

New app `handling`.

| File | Responsibility |
|---|---|
| `handling/models.py` | `HandlingSettings`, `HandlingRate`, `HandlingRun`, `HandlingRunLine` |
| `handling/profit.py` | Profit and handling owed for one order or all orders, as of a date. Reads documents only; posts nothing. |
| `handling/periods.py` | Half-month periods: the period containing a date, the next period to bill, period labels. |
| `handling/billing.py` | Preview a period, create the bill or vendor credit, void the latest run. |
| `handling/checks.py` | Month close check; lock check for handling bills. |
| `handling/views/`, `templates/`, `static/` | Report, order panel, preview, statement PDF, settings. |

## 4. Data model

| Model | Fields |
|---|---|
| `HandlingSettings` (one row) | vendor (contacts.Vendor), expense_account (ledger.Account, an expense account), start_date (orders dated before it never get handling) |
| `HandlingRate` | effective_from (date, unique), rate (Decimal, 4 places, between 0 and 1; 0.5500 = 55%), created_by, created_at |
| `HandlingRun` | period_start, period_end (unique together), total (Decimal, may be negative or 0.00), bill (purchasing.Bill, nullable), vendor_credit (purchasing.VendorCredit, nullable), status (POSTED, VOID), statement_pdf (file under `/data/media`), created_by, created_at, voided_by, voided_at, void_reason |
| `HandlingRunLine` | run, order_key (text, `SO-00123` or `INV-00456`), sales_order (nullable), invoice (nullable, set for a direct invoice), customer, sales, cost, profit, rate, owed_to_date, billed_before, this_bill (all Decimal 2 places except rate) |

`order_key` is stable: the sales order number, or the invoice number for a direct invoice.

## 5. Rules

### 5.1 Profit for one order

Profit is computed as of a date (`as_of`). Only documents dated on or before `as_of` count.

- **The order.** A sales order with every invoice that links to it. Or a single invoice with no sales order.
- **Sales.**
  - The sum of the order's invoice line amounts, at the invoice's current revision. An invoice voided on or before `as_of` counts as 0.00.
  - Less the credit note applications on those invoices: `applied_on` on or before `as_of`, and not removed on or before `as_of`. If a credit note is voided, its applications are gone from the void date.
- **Cost.**
  - Stock lines: the current `total_cost` of the stock movements recorded for the order's invoices (`source_type = "sales.invoice"`, `source_id` = invoice number), after any FIFO replay. Voided movements count as 0.00 from their void date.
  - Drop-ship lines: for each invoice line, the cost of the drop-ship bill lines allocated to it (`DropShipAllocation`) on bills dated on or before `as_of` and not void. A drop-ship line with no allocation yet costs 0.00 and is flagged "Drop-ship bill not entered".
- **Profit** = sales − cost.
- **Rate.** The `HandlingRate` in effect on the order date: the sales order's date, or the direct invoice's date. One order never mixes two rates.
- **Handling owed to date** = max(0.00, profit × rate), rounded half-up to the cent.
- An order dated before `HandlingSettings.start_date` owes 0.00 and is not listed on runs. The report shows it with the label "Before handling start".

### 5.2 Periods

- Two periods each month: the 1st to the 15th, and the 16th to the last day.
- A period can be billed on or after its last day.
- Periods are billed in order with no gaps, starting with the period that contains `start_date`.
- Each period is billed once. A void run makes its period billable again, after which later periods continue in order.

### 5.3 Creating a handling bill

`create_run(period_end, user, token)`:

1. Refuse if the settings are incomplete ("Set up handling in Purchasing settings first.").
2. Refuse if this is not the next period to bill ("Bill {label} first." or "{label} is already billed."). Refuse if `period_end` is in the future ("{label} can't be billed until {date}.").
3. For every order dated on or after `start_date` and on or before `period_end`:
   - `owed_to_date` = handling owed as of `period_end` (5.1).
   - `billed_before` = the sum of `this_bill` on the order's lines in earlier runs that are not void.
   - `this_bill` = `owed_to_date` − `billed_before`. This is negative when a credit, a late drop-ship bill, or an edit lowered the profit.
   - Orders with `this_bill` = 0.00 are left off the run.
4. `total` = sum of `this_bill`. Then:
   - Above 0.00: create one bill to the handling vendor, dated `period_end`, through `purchasing.posting.bills.create_bill`. It has one expense line to the handling expense account for `total`, described "Handling {label}". The vendor invoice number is "HND-{period_end:%Y%m%d}". The bill's notes link the statement.
   - Below 0.00: create a vendor credit for the absolute amount, dated `period_end`, through `create_allowance_credit`, with reason "Handling corrections {label}". It is applied to the handling vendor's open bills like any other credit.
   - 0.00: record the run with no bill and no credit, so the period counts as billed.
5. Save the run, its lines, and the statement PDF. One transaction. A second click with the same form token does nothing ("That was already done.").

Labels: "Oct 1–15, 2026" and "Oct 16–31, 2026".

### 5.4 Voiding

- Only the most recent run that is not void can be voided. Admin only, with a reason.
- Voiding a run voids its bill (through `void_bill`) or its vendor credit (through `void_vendor_credit`), then marks the run VOID. If the bill has payments applied, it can't be voided ("Remove the payments on {bill} first."), which is the existing Phase 4 rule.
- A handling bill can't be voided or edited from the Bills screens unless it belongs to the latest run. A lock check registered with `ledger.locks` returns "{bill} is the handling bill for {label}. Void it from Handling." for the bill of any run that is not void. `void_run` passes it by voiding its own bill inside the run's transaction, after marking the run VOID.

### 5.5 Month close

A close check, registered with `ledger.periods`. A month can't close until both of its periods have a run that is not void:

"{Month YYYY}: the handling bill for {label} hasn't been created."

The check applies only to months on or after `start_date`'s month. An admin can override it with a reason, as with the other close checks.

### 5.6 Rate changes

- Adding a rate is admin only. `effective_from` must be after the latest existing rate's date.
- Rates are never edited or deleted once any run has used them. A mistake is fixed by adding a new rate.
- The first rate (0.5500) is created by the settings form with `effective_from` = `start_date`.

## 6. Screens

All match "Ledger paper", with the Auto/Light/Dark toggle.

| Screen | What it shows |
|---|---|
| **Order profitability** (Reports) | One row per order: order, customer, date, sales, cost, profit, margin %, handling owed, handling billed, balance (owed − billed). Filters: date range (default this month), customer, "Has unbilled handling". Flags: "Drop-ship bill not entered", "Below zero, no handling", "Before handling start". Totals row. Each order links to its page. |
| **Profit panel** (on each sales order, and on direct invoices) | Sales by invoice, credits applied, cost by line (stock or drop-ship), profit, rate, handling owed, and each handling run that billed it, with its amount. |
| **Handling** (Purchasing) | List of runs: period, total, bill or credit, status. A "Next: {label}" button, enabled once the period has ended. |
| **Preview** | The rows the run would create: order, customer, sales, cost, profit, owed to date, billed before, this bill. Total at the bottom. The **Create bill** button (or **Create credit** when the total is negative). Shows the settings problem instead when they are incomplete. |
| **Run detail** | The saved rows, links to the bill or credit, the statement PDF, and the Void button for the latest run (admin). |
| **Statement PDF** | "Handling statement, {label}", DNB's name, the handling company's name, the rows, and the total. Same style as the invoice PDF. |
| **Settings** (Purchasing settings, Handling section) | Handling vendor, expense account, start date, current rate, rate history, Add rate (admin). |
| **Home card** | "Handling bill due for {label}" with a link to the preview, shown once a period has ended and is unbilled. |

## 7. Permissions

- Viewing the report, the profit panel, and runs: all users.
- Creating a run: all users. Standard users can pay bills, and this is the same kind of task.
- Voiding a run, settings, rates: admin only.

## 8. Testing

| Area | Tests |
|---|---|
| Profit | Stock order; drop-ship order; mixed; partly shipped over two invoices; direct invoice; invoice edited (new revision counts); invoice voided; credit applied, removed, moved to another order's invoice; credit voided; drop-ship line with no bill (0.00 cost, flagged); FIFO replay after a backdated receipt changes cost; `as_of` excludes later documents. |
| Handling owed | 55% rounding half-up; loss held at 0.00; rate by order date across a rate change; order before start date owes nothing. |
| Runs | Periods in order, no gaps, not before their end; second click does nothing; total above, below, and at 0.00 (bill, vendor credit, no document); late drop-ship bill corrected in the next run; void only the latest; void refused with payments applied; a handling bill can't be voided or edited from the Bills screens; voiding then re-billing a period. |
| Ledger | After a scripted month, handling bills less vendor credits equals the Handling expense account balance; A/P check stays at 0.00. |
| Close | Month blocked until both periods are billed; override with a reason; months before the start date not checked. |
| Screens | Report filters and totals; profit panel; preview; run detail; settings; home card. Standard user can create a run, not void one. |

## 9. Notes

- **Reads documents, not the ledger.** The ledger does not record which order a line belongs to. So `handling/profit.py` reads invoices, credit applications, stock movements, and drop-ship allocations directly, as the A/R aging does. This is an exception to "reports read only from posted ledger lines", agreed with the owner. Money only reaches the ledger through the handling bill, which posts through `purchasing` and `ledger/posting.py` as usual.
- **Handling expense account.** Use a dedicated expense account so success criterion 2 holds.
- **Zoho migration (Phase 5b).** Set `start_date` to the first day DNB books handling in this system. Orders imported from Zoho dated earlier are never billed.
