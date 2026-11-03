# Zoho-style invoice and statement PDFs

- **Date:** 2026-11-03
- **Status:** Decisions made by the owner (chat, 2026-11-03); written spec follows them.
- **Parent specs:** `2026-10-27-phase-3-sales-design.md` (invoice PDF), `2026-11-03-customer-statements-design.md` (statement). Where they differ on layout, this document wins.
- **References (owner's real Zoho PDFs, never committed):** an invoice (2 pages) and a customer statement. Visual reference in the repo: `docs/design/invoice-mockup.html`, updated to this layout.

## 1. Goal

The invoice and statement PDFs look like the owner's Zoho documents, so customers see the same paperwork after the cutover.

## 2. Decisions from the owner

| Topic | Decision |
|---|---|
| Palette | Black, white, and grey like Zoho (the green accent is dropped from these two PDFs). |
| Invoice header | Logo and company beside it at top left, "INVOICE" at top right. A boxed block: Invoice#, Invoice Date, Terms, Due Date, P.O.# (the sales order number; blank row when there is none). Then Bill To and Ship To boxes (Ship To falls back to the Bill To address, as today). |
| Invoice table | `#`, Item & Description, Packaging, Qty (unit under the number, e.g. CASE), Price, Amount, Total. Amount and Total are both kept, as in Zoho; with no tax or discounts they are equal. Lines are grouped under the item's category as a heading row whenever the category changes; no heading for uncategorized items. |
| Invoice footer | "Items in Total N" (sum of quantities), the invoice notes at left; at right a box with Total, Payments and credits (only if any), Balance Due; an Authorized Signature box. Page number bottom right. |
| Packaging | New optional text field on each item (for example 25X16OZ), edited on the item screen. Not part of the CSV import in this change. |
| Logo | Existing logo upload in Sales settings. The owner supplies the file; it is never committed. |
| Statement | Logo top left, company details top right, "To" block, "Statement of Accounts" with the date range, an Account Summary box (Opening Balance, Invoiced Amount, Amount Paid, Balance Due), a dark-header table (Date, Transactions, Details, Amount, Payments, Balance) with an Opening Balance row and a Balance Due line, and an Aging Summary strip. Rows stay invoice-first: each invoice with its payments and credits beneath it at the amount applied to that invoice (owner's earlier decision). Unapplied payments and credits follow. |
| Account Summary | Opening + Invoiced − Paid = Balance Due. Invoiced = invoices in the range less any voided in the range. Paid = payments and credit notes in the range, less refunds and voids of payments or credits. |

## 3. Out of scope

The pay-by-QR code and online payment ("Scan the QR code to view and pay"), emailing, tax, discounts, packing slips, loading Packaging from the Zoho item export (a follow-up for the migration session).

## 4. Testing

Rendered HTML and PDF tests for each element above; category headings appear only on change; Items in Total; Packaging field on the item form; Account Summary reconciles to the Balance Due and to the A/R aging row; totals unchanged from before.
