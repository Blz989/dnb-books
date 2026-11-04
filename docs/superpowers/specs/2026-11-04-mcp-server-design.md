# DNB Books MCP server: design

Status: draft for owner review. Date: 2026-11-04.

## 1. Goal

Let the people who use DNB Books work with it from Claude (Claude Desktop, claude.ai on the web, and the Claude phone app). Claude can look things up and run reports, and can create invoices, payments, bills, vendor payments, purchase orders, and contact and item records, with each change confirmed before it posts.

## 2. Owner decisions

| Topic | Decision |
|---|---|
| Where it's used | Claude Desktop, claude.ai, and phone. All three use one remote connector. |
| Network | Cloudflare Tunnel on a domain the owner manages in Cloudflare. Both stages are built now, not staged. |
| Sign-in | Each person signs in with their own DNB Books login (OAuth). Their app role applies. |
| What Claude can do | Read, plus guarded writes: invoices and payments, bills and vendor payments, purchase orders, customers, vendors, items. |
| Who can write | The same as each person's app role. |
| Library | The official MCP Python SDK (`mcp`, 2.x). No other new dependency. |

The owner chose internet access before cutover, against the recommendation to wait. Section 7 lists the controls that make this acceptable, and section 10 lists what the owner sets up.

## 3. Architecture

```
 Claude (Desktop / web / phone)
        │  HTTPS, from Anthropic's cloud
        ▼
 Cloudflare ──► cloudflared (container) ──► mcp service :8001  (new container)
                                              │  same code and database as the app
                                              ▼
                                           SQLite (data/dnb.sqlite3)
 Office browsers ───────────────────────────► app :8000  (unchanged, office network only)
```

- **A new `mcp` service** in docker-compose runs `manage.py serve_mcp`: an ASGI app (the SDK's `MCPServer` with Streamable HTTP, served by uvicorn, which the SDK already brings). It imports Django and calls the same services the screens use. It doesn't call the web app over HTTP.
- **Only the `mcp` service is published through the tunnel.** It serves the MCP endpoint (`/mcp`), the OAuth endpoints, and its own sign-in and approval pages. The main app on port 8000 is not reachable from the internet.
- **A new Django app, `mcpserver`,** holds the models, the OAuth provider, the tools, and the screens in Settings.

## 4. Sign-in (OAuth)

The SDK's `OAuthAuthorizationServerProvider` is implemented against Django models, so tokens live in the DNB Books database.

1. In Claude, the person adds a custom connector with the URL `https://<mcp domain>/mcp`.
2. Claude registers itself automatically (dynamic client registration). Registration accepts only Claude's callback URLs (`https://claude.ai/api/mcp/auth_callback` and `https://claude.com/api/mcp/auth_callback`). Any other redirect is refused.
3. Claude opens the DNB Books sign-in page. The person enters their DNB Books username and password, then approves: "Allow Claude to read your books and, after you confirm each change, create invoices, payments, bills, purchase orders, and records, as {username} ({role})."
4. Claude receives tokens. PKCE is required. Authorization codes last 5 minutes and are single use. Access tokens last 1 hour; refresh tokens last 30 days and rotate on use. Tokens are stored only as SHA-256 hashes.
5. Every MCP request carries the access token. The service loads that user, and the user must still be active.

**Settings → Claude connections** (admin) lists every connection: user, connected on, last used, last tool. Any connection can be revoked, and an on/off switch turns the whole MCP service off. Each person also sees and can revoke their own connections on their user page. Turning a user's "Can sign in" off also stops their MCP access.

## 5. Tools

Every tool runs as the signed-in user, with the same permission checks as the matching screen. Inputs refer to records by their visible numbers and names (INV-000123, SKU, customer name), never internal ids. Amounts are strings like "125.00" and are parsed as Decimal.

### Read

| Tool | Returns |
|---|---|
| `home_summary` | The home page figures: open and overdue A/R, bills due, cash today, stock value, items to reorder, inbox waiting |
| `search_customers`, `get_customer` | Matching customers; one customer's balance, terms, open invoices, recent payments |
| `search_vendors`, `get_vendor` | Matching vendors; one vendor's balance, open bills, recent payments (tax ID always masked) |
| `search_items`, `get_item` | Matching items; one item's on hand, on order, reorder point, cost, prices |
| `list_invoices`, `get_invoice` | Filter by customer, open or overdue, date range; one invoice's lines, payments, balance |
| `list_bills`, `get_bill` | Filter by vendor, due through, open or overdue; one bill's lines and payments |
| `list_purchase_orders`, `get_purchase_order` | Open or all; one PO's lines with billed and remaining quantities |
| `search_journal_entries` | Like the Journal entries screen |
| `run_report` | One of: trial balance, profit and loss (accrual or cash, compare up to 3 years), balance sheet (compare), A/R aging, A/P aging, open invoices, bills due, low stock, inventory valuation, order profitability, cash forecast (daily, weekly, or monthly) |

Lists return at most 50 rows and say when more exist.

### Write: preview, then confirm

A write tool never changes anything on its own call. It validates the input with the same checks the posting service uses, and returns a preview (what will be created, totals, the accounts it posts to, any warnings such as "stock goes below zero") plus a `confirmation_id`. Nothing posts until Claude calls `confirm_change(confirmation_id)`.

- A confirmation is single use, expires after 15 minutes, and belongs to the user who previewed it.
- It stores the exact validated input. Confirming re-runs validation, so a month closed in between still blocks it with the usual message.
- Write tools carry the MCP annotation `readOnlyHint: false`, which Claude apps use when deciding whether to ask before running a tool. The preview and confirm step protects the books whether or not the app asks.

| Tool | Posts through | Notes |
|---|---|---|
| `create_invoice` | `sales.posting.invoices.create_invoice` | Customer, date, lines (SKU, quantity, optional price and description), optional sales order, ship-to, notes. Drop-ship lines allowed, as in the app. |
| `receive_payment` | `sales.posting.receipts.receive_payment` | Customer, date, method, amount, bank account for ACH and wire, and which invoices it pays. |
| `create_bill` | `purchasing.posting.bills.create_bill` | Vendor, date, vendor invoice number, item lines or expense lines, optional PO. The duplicate-bill warning shows in the preview. |
| `pay_bills` | `purchasing.posting.payments.pay_vendor` | Vendor, date, method, paid from, amount, which bills it pays. A check payment is handled the same way as one entered on the Pay bills screen. |
| `create_purchase_order` | `purchasing.posting.orders.create_po` | Vendor, date, lines, expected date, notes. |
| `save_customer`, `save_vendor`, `save_item` | The same forms the screens use | Create, or update by name or SKU. A new item gets the next SKU in the series when none is given. Vendor tax IDs can be set but are never returned. |

**Not available through the MCP:** voids, edits to posted documents, refunds, credit notes, deposits, the bank inbox, reconciliation, transfers, journal entries, month close, settings, users. These stay in the app.

## 6. Plain-language errors

Errors from the posting services pass through unchanged (for example "October 2026 is closed. Choose a date in an open month or ask an admin to reopen it."). New messages:

| Case | Message |
|---|---|
| Unknown record | "No customer named Acme Bulders. Did you mean Acme Builders?" (the closest names, when any) |
| Not allowed | "Your role (Standard) can't do this. Ask an admin." |
| Expired confirmation | "That preview expired. Ask for it again." |
| Used confirmation | "That change was already made." |
| MCP turned off | "Claude access to DNB Books is turned off. An admin can turn it on in Settings." |

## 7. Security controls

| Risk | Control |
|---|---|
| The books are on the internet | Only the `mcp` service is published; the app stays on the office network. Cloudflare sits in front. A Cloudflare rule limits `/mcp` to Anthropic's published IP ranges. The sign-in page must stay reachable from phones, so it is not IP-limited. |
| Password guessing on the sign-in page | 5 failed tries per username, or 20 per IP, in 15 minutes locks sign-in for 15 minutes. Locks and failures are logged. |
| Another app impersonating Claude | Registration only accepts Claude's callback URLs; PKCE is required; codes are single use. |
| A stolen token | Short access tokens, rotating refresh tokens, hashes at rest, revoke in Settings, and the on/off switch. |
| A wrong posting from chat | Preview and confirm on every write; no voids through the MCP; closed months enforced; every write recorded in the audit log under the person's name. |
| Unclear activity | A `McpCall` log: user, tool, time, success or error, and the document created. Shown in Settings → Claude connections. Tool arguments are logged with tax IDs removed. |

## 8. Ledger rules

The MCP adds no new posting paths. Every write calls the existing services, which go through `ledger/posting.py`. Reports come from the existing report functions. Money is Decimal throughout.

## 9. Testing

- OAuth provider: registration accepts only Claude callbacks; a code exchanges once; PKCE is enforced; expiry, refresh rotation, and revocation work; inactive users are refused; sign-in lockout.
- Every tool: works for Admin; Standard gets the role message where the app would refuse; unknown-record messages.
- Preview and confirm: nothing posts on preview; confirm posts exactly the preview; expired, reused, and other-user confirmations are refused; a month closed between preview and confirm blocks it.
- Audit: writes record the person as the actor.
- End to end: the SDK's client against the ASGI app in-process (no network). It signs in, lists tools, runs a report, previews and confirms an invoice.
- Screenshots: sign-in page, approval page, Settings → Claude connections.

## 10. What the owner sets up

1. A domain in Cloudflare (for example `mcp.yourcompany.com`).
2. In Cloudflare Zero Trust, create a tunnel and copy its token into `.env` as `CLOUDFLARE_TUNNEL_TOKEN`. Set `DNB_MCP_PUBLIC_URL=https://mcp.yourcompany.com`.
3. Add the WAF rule limiting `/mcp` to Anthropic's IP ranges (the README links Anthropic's page).
4. `docker compose up -d --build` starts the `mcp` and `cloudflared` services.
5. In Claude: Settings → Connectors → Add custom connector → paste the URL → sign in.

## 11. Out of scope

A local stdio server for Claude Desktop (the remote connector covers Desktop too); two-step sign-in (recommended as a follow-up before wide use); MCP access to the items listed in section 5 as not available; email or notifications.

## 12. Build order

1. `mcpserver` app: models (OAuth clients, codes, tokens, pending confirmations, call log, lockouts), the provider, sign-in and approval pages, `serve_mcp`.
2. Read tools.
3. Write tools with preview and confirm.
4. Settings → Claude connections, revoke, on/off.
5. docker-compose services, `.env.example`, README setup section.
