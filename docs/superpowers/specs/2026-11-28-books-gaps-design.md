# Attachments, sales and purchase reports, recurring transactions, cash flow statement, bad debt write-off: design

Status: approved by the owner in chat on 2026-11-28 (choices below); in review.

Five features that Zoho Books and QuickBooks Online have and DNB Books does not. Each is independent; they ship in one PR.

## Owner decisions (2026-11-28)

| Question | Answer |
|---|---|
| Sales and purchase reports by item | Read invoice and bill lines, and prove the total against the ledger, the way A/R aging does. |
| Recurring: what happens on the due day | Create and post automatically. Home lists what was created. |
| Recurring: which types | Bills and journal entries. Not invoices. |
| Recurring bills: which lines | Expense lines only. No stock moves. |
| Who can write off bad debt | Admin only. |
| Does a write-off lower order profit (and handling owed) | Yes, the same way a credit note does. |

## 1. Attachments

Files kept with a record: the vendor's bill PDF, a receipt, a W-9, signed proof of delivery.

### 1.1 Where
An Attachments card on the detail page of: invoices, sales orders, credit notes, customer payments, bills, purchase orders, vendor credits, vendor payments, journal entries, customers, and vendors.

### 1.2 Rules
- Any signed-in user can add a file. Only an admin can remove one.
- Allowed: PDF, PNG, JPEG. Checked by the file's first bytes, not only its name. Up to 10 MB each.
- Files live under `MEDIA_ROOT/attachments/<yyyy>/<mm>/` with a random stored name; the original name is kept for display and download.
- Opening a file needs sign-in. Files open in the browser (`inline`) with `X-Content-Type-Options: nosniff`.
- Adding and removing are in the audit log (who, when, file name, which record).
- Attachments are allowed on void and deleted documents (the proof often matters most then), and on documents in closed or reconciled months: a file is not a ledger change.
- Removing deletes the stored file and the record. The audit log keeps the name and size.
- Backups already include `MEDIA_ROOT`, so attachments are backed up and restored with no change.

### 1.3 Screen
The card lists name, size, who added it, and when, each name a link that opens the file. Below it: a file picker and Add file. Admins see Remove on each row, with a confirm step.

### 1.4 Messages
- "Choose a file to add."
- "Only PDF, PNG, and JPEG files can be added."
- "That file is over 10 MB. Save it smaller and try again."
- "Only an admin can remove an attachment."

### 1.5 Out of scope
Attaching through Claude (MCP), scanning text from receipts, attachments on bank inbox rows, previews or thumbnails.

## 2. Sales and purchase reports

Four reports, each with a date range (default this month to date), CSV, and PDF, listed in the reports catalog.

| Report | Rows | Columns |
|---|---|---|
| Sales by customer | One per customer with activity | Invoices (count), Sales, Credits, Net sales, Written off |
| Sales by item | One per item sold | Quantity, Sales, Average price |
| Purchases by vendor | One per vendor with activity | Bills (count), Purchases, Vendor credits, Net purchases |
| Purchases by item | One per item bought, then one per expense account | Quantity, Amount, Average cost |

The catalog gets a new group "Sales and purchases" between Business overview and Receivables holding all four.

### 2.1 Where the numbers come from
- Sales by customer reads posted ledger lines on income accounts (Income and Other income subtypes) from entries whose source is an invoice or credit note, grouped by the document's customer. Sales = income credits less income debits on invoice entries (edits and voids net out by date, as in the ledger). Credits = income debits less income credits on credit note entries. Credit note lines on non-income accounts are not sales. Written off = the bad debt lines of write-off credit notes (section 5), shown for information; it is not in Net sales.
- Purchases by vendor reads A/P lines from bill and vendor credit entries the same way.
- Sales by item reads invoice lines on invoices dated in the range, with every status, plus, for each invoice voided within the range, its lines again with negative quantity and amount. This matches how the ledger books a void (the original stays, a reversal posts on the void date). Deleted invoices were voided first, so they follow the same rule. Adjustment lines show as the item they post to (ADJ).
- Purchases by item reads bill item lines and bill expense lines the same way (bill date, plus negative on the void date).

### 2.2 Tie-out (the ledger check)
Each report ends with a check block:
- Sales by item: "Invoice total in the ledger" (A/R debits less credits on invoice entries in the range) and "Difference", which must be 0.00.
- Purchases by item: "Bill total in the ledger" (A/P credits less debits on bill entries in the range) and "Difference", 0.00.
- Sales by customer: "Income from other entries" (income lines from journal entries, bank entries, and anything else not an invoice or credit note), "Income on the profit and loss" for the range, and "Difference" = P&L income less (Net sales + income from other entries), which must be 0.00.
- Purchases by vendor: no P&L tie-out (purchases include stock, which is an asset); the check is "Bills and credits in A/P" against the report total, 0.00.

A difference other than 0.00 shows in red with "This should be 0.00. Tell the person who maintains DNB Books." That is a bug, not a user error.

### 2.3 Filters
Date range on all four. Customer on Sales by item; vendor on Purchases by item. Clicking a customer or vendor name opens their page; clicking an item opens the item.

### 2.4 Out of scope
Sales by salesperson, profit by item (order profitability covers profit), comparisons with prior years.

## 3. Recurring bills and journal entries

### 3.1 Profiles
A recurring bill holds: name, vendor, terms (default the vendor's), notes, expense lines (account, description, amount), frequency, start date, optional end date, and active or paused.

A recurring journal entry holds: name, memo, lines (account, description, debit or credit), frequency, start date, optional end date, active or paused.

Frequencies: Weekly, Every 2 weeks, Monthly, Every 3 months, Every 6 months, Yearly. Monthly-type dates are counted from the start date: occurrence n is start date plus n months, on the same day, or the month's last day when the month is shorter (start Jan 31: Feb 28, Mar 31, Apr 30). Weekly-type dates add 7 or 14 days.

### 3.2 Running
- The scheduler runs `run_recurring` every day at `DNB_RECURRING_TIME` (default 05:00) and once when it starts.
- For each active profile, every occurrence dated on or before today and on or before the end date that has not been created is created and posted, oldest first, dated on its scheduled date. A missed day (server off) catches up.
- Each created document is recorded against the profile with its scheduled date. The same profile and date can never be created twice (database unique constraint).
- Bills are created with `create_bill` (no vendor invoice number, notes "From recurring bill <name>" followed by the profile notes). Journal entries with `post_entry`, reference "Recurring", memo the profile memo, `source_type` blank (a manual entry, voided the usual way).
- They are posted as the user who created the profile.
- When a run fails (a closed month, an inactive account, a vendor made inactive), that profile stops at the failed date, saves the plain error and the date, and tries again the next run. Other profiles still run.
- Pausing stops new runs. Resuming does not create the dates passed while paused: the next date becomes the first scheduled date on or after the resume day.
- A profile past its end date shows as Ended.

### 3.3 Screens
- Purchasing, Recurring bills: list (name, vendor, frequency, amount, next date, status), New, a profile page (details, its created bills with links, last error), Edit, Pause or Resume, Delete (only when nothing has been created; otherwise set an end date).
- Accountant, Recurring journal entries: the same for journal entries. Debits must equal credits when saving.
- Edits change future runs only. Documents already created are edited or voided the usual way.
- Home: a card "Created by recurring schedules" listing the last 7 days of created bills and entries, and a warning banner for each profile with an error: "Recurring bill Rent couldn't be created for 2026-12-01: <message>".

### 3.4 Cash forecast
Upcoming recurring bill occurrences (after today, through the forecast horizon) are added as outflows on their dates, labeled "Recurring: <name>". The forecast's note changes from "Rent, payroll, and other costs count only once they're entered as bills" to "Recurring bills count on their next dates; other costs count once they're entered as bills."

### 3.5 Permissions
Recurring bills: any user (like bills). Recurring journal entries: the same users who can post a journal entry today.

### 3.6 Messages
- "Add at least one line."
- "Line <n>: enter an amount above zero."
- "Line <n>: choose an account."
- "Line <n>: choose an account other than accounts payable." (recurring bills; inventory accounts are also refused: "Line <n>: inventory changes through stock movements. Choose an expense account.")
- "The end date can't be before the start date."
- "Debits (<x>) and credits (<y>) must be equal. Difference: <z>." (the existing ledger message)
- "This schedule has created documents. Set an end date instead of deleting it."

### 3.7 Out of scope
Recurring invoices, recurring bills with item lines, emailing, amounts that change each period.

## 4. Statement of cash flows

Reports, Business overview, "Cash flow statement", date range (default this year to date), CSV, PDF. Indirect method, read only from posted ledger lines.

| Section | Lines |
|---|---|
| Operating activities | Net income (from the accrual P&L for the range), then the change in each account with subtype Accounts receivable, Inventory, Other current asset, Accounts payable, Credit card, Other current liability |
| Investing activities | Change in each Fixed asset and Other asset account |
| Financing activities | Change in each Long term liability and Equity account |

The change for each account is the movement of its balance within the range, signed for cash: an asset going up uses cash (negative), a liability or equity going up provides cash (positive). Accounts with no movement are left out.

Below: Net change in cash, Cash at the start (Bank subtype accounts, balance on the day before the start), Cash at the end, and a check line "Change in bank accounts" with "Difference" that must be 0.00.

Credit cards are operating, as QuickBooks does. Undeposited funds follows its subtype (Other current asset, so operating).

Out of scope: direct method, comparisons with prior years, per-account reclassification.

## 5. Bad debt write-off

### 5.1 Flow
An admin opens an invoice with a balance and presses Write off. The form has: date (default today, not before the invoice date), amount (default the balance, at most the balance), and reason (required). Saving, in one transaction:
1. Creates a credit note for the customer with one line to the bad debt account for the amount, reason "Bad debt: <reason>", marked as a write-off.
2. Applies it to the invoice on the same date.

The ledger shows Bad debt expense debited and A/R credited. The invoice is paid (or partly paid) by the write-off. The customer's statement, A/R aging, and the invoice page show it as a credit note "Written off".

### 5.2 Setting
Settings, Sales: "Bad debt account" (expense accounts only). If it is blank, Write off shows: "Set the bad debt account in Settings, Sales first."

### 5.3 Undo
Remove the application on the invoice, then void the credit note (the existing flow, admin). No new undo screen.

### 5.4 Effects elsewhere
- Order profitability: counts like any credit applied to the invoice, so profit and handling owed go down (owner decision). A handling bill already created is corrected by the next one.
- Cash-basis P&L: a write-off counts nothing. Its bad debt line is not an expense on the cash basis (the income was never counted), and its application does not count as the invoice being paid.
- Sales by customer: shown in Written off, not Credits (section 2.1).
- Refunds can't be drawn from a write-off credit note: "A write-off can't be refunded."
- A write-off credit note can't be applied to a different invoice: "A write-off applies only to the invoice it was made for."

### 5.5 Messages
- "Only an admin can write off an invoice."
- "Enter a reason."
- "Enter an amount above zero."
- "<number> has <balance> left to pay." (existing)
- "The write-off date can't be before the invoice date."
- "<number> is void and can't be written off."

### 5.6 Out of scope
Recovering a written-off debt in one step (record the payment and void the write-off), write-offs through Claude.
