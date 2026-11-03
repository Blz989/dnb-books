# Zoho-style PDFs Implementation Plan

> Inline execution, TDD. Spec: `docs/superpowers/specs/2026-11-03-zoho-style-pdfs-design.md`. PR title: `feat: invoice and statement PDFs match the Zoho layout`

**Global constraints:** Decimal money; no ledger writes; no new dependencies; screenshots from a demo database only.

- [ ] Task 1: `Item.packaging` field, migration, item form and detail. Commit: `feat: items have a packaging note`
- [ ] Task 2: Invoice PDF layout (header, boxes, table with category headings, footer, signature, page number). Commit: `feat: invoice PDF matches the Zoho layout`
- [ ] Task 3: Statement PDF layout and Account Summary figures (`Statement.invoiced`, `Statement.paid`). Commit: `feat: statement PDF matches the Zoho layout`
- [ ] Task 4: Update `docs/design/invoice-mockup.html`, README; send screenshots. Commit: `docs: invoice mockup matches the new layout`
