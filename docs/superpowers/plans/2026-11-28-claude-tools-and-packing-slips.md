# Claude tools for the new features, and packing slips: plan

Spec: `docs/superpowers/specs/2026-11-28-claude-tools-and-packing-slips-design.md`.
PR title: "Claude tools for reports, write-offs, and recurring schedules; packing slips"

## Global Constraints
- No new dependencies.
- MCP tools call the same functions the screens call; no new posting code. Writes go through `write.preview` and `write.confirm`.
- Existing tests pass untouched.
- Screenshots from a throwaway demo database; never `data/`. The packing slip is screenshotted as a rendered PDF.

## Review Focus
- Write-off through Claude is admin only and the preview rolls back (no credit note left behind).
- Recurring create previews leave no schedule behind; duplicate names are refused, not guessed.
- The packing slip shows no money anywhere.

## Task 1: Reports in `run_report`
- [x] `mcpserver/read_reports.py`: `sales_by_customer`, `sales_by_item`, `purchases_by_vendor`, `purchases_by_item`, `cash_flow` in `EXTRA`, with `customer` and `vendor` inputs passed through `run_report`.
- [x] `mcpserver/tools.py`: report names and the two inputs added to `run_report`, docstring updated.
- [x] Tests `tests/test_mcp_books_gaps.py`: each report's figures equal the screen function's; unknown customer refused.
- [x] Commit: `Claude tools: sales and purchase reports and the cash flow statement`

## Task 2: Write-off and recurring tools
Interfaces:
```python
# mcpserver/write_gaps.py
OPERATIONS = {"write_off_invoice": ..., "create_recurring_bill": ..., "create_recurring_journal_entry": ...,
              "pause_recurring": ..., "resume_recurring": ...}
def list_recurring(user) -> dict
```
- [x] Operations registered in `write.OPERATIONS`; tools in `tools.py`.
- [x] Recurring line checks reuse `ledger.recurring_views.bill_lines` / `journal_lines` (rows built from the tool input).
- [x] Tests in `tests/test_mcp_books_gaps.py`: preview saves nothing, confirm saves exactly the preview, admin-only write-off, app messages pass through, duplicate names refused, pause and resume.
- [x] Commit: `Claude tools: write-offs and recurring schedules`

## Task 3: Packing slips
- [ ] `sales/pdf.py`: `render_packing_slip_html(invoice)`, `packing_slip_pdf(invoice)`; template `sales/packing_slip_pdf.html` and stylesheet reusing the Classic invoice CSS.
- [ ] View and URL; Packing slip button on the invoice page.
- [ ] Tests `tests/test_packing_slip.py`: stock lines and quantities in, no prices or totals, drop-ship note, drop-ship-only invoice, adjustment line left out, void mark.
- [ ] Screenshots: invoice page with the button, packing slip PDF (stock and drop-ship), drop-ship-only slip.
- [ ] Commit: `Packing slips`

## Task 4: Docs and PR
- [ ] README rows; Status line; CLAUDE.md source of truth.
- [ ] Full `uv run pytest -q`.
- [ ] Commit: `Docs: Claude tools and packing slips`; open the PR.
