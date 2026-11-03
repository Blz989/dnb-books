# Zoho-style PDFs Implementation Plan

> Inline execution, TDD. Spec: `docs/superpowers/specs/2026-11-03-zoho-style-pdfs-design.md`. PR title: `feat: invoice and statement PDFs match the Zoho layout`

**Global constraints:** Decimal money; no ledger writes; no new dependencies; screenshots from a demo database only.

- [x] Task 1: `Item.packaging` field, migration, item form and detail. Commit: `feat: items have a packaging note`
- [x] Task 2: Invoice PDF layout (header, boxes, table with category headings, footer, signature, page number). Commit: `feat: invoice PDF matches the Zoho layout`
- [x] Task 3: Statement PDF layout and Account Summary figures (`Statement.invoiced`, `Statement.paid`). Commit: `feat: statement PDF matches the Zoho layout`
- [x] Task 4: Update `docs/design/invoice-mockup.html`, README; send screenshots. Commit: `docs: invoice mockup matches the new layout`

**Added at the owner's request during the build:** Packaging loads from the item CSV import (optional `packaging` column), the Zoho `Item.csv` (`CF.Packaging` or `Packaging`), and `backfill_packaging <Item.csv>` fills it on items already imported. Lines group by category once each.
