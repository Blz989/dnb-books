# DNB Books MCP server: design

Status: draft for owner review, revision 3 (fits the existing LopezHome tunnel and Access setup). Date: 2026-11-04.

## 1. Goal

Let the people who use DNB Books work with it from Claude (Claude Desktop, claude.ai on the web, and the Claude phone app). Claude can look things up and run reports, and can create invoices, payments, bills, vendor payments, purchase orders, and contact and item records, with each change confirmed before it posts.

## 2. Owner decisions

| Topic | Decision |
|---|---|
| Where it's used | Claude Desktop, claude.ai, and phone. All three use one remote connector. |
| Network | The existing remotely managed `LopezHome` tunnel on `lopezhome.casa`. A new public hostname `mcp.lopezhome.casa` points to `http://192.168.0.33:8001`, behind its own Access application. It can also be listed in the owner's Cloudflare MCP server portal. |
| Sign-in | Cloudflare Access does the sign-in (Managed OAuth) through the owner's identity provider, so two-step sign-in comes from there. DNB Books maps the signed-in email to a DNB Books user, and that user's role applies. |
| What Claude can do | Read, plus guarded writes: invoices and payments, bills and vendor payments, purchase orders, customers, vendors, items. |
| Who can write | The same as each person's app role. |
| Library | The official MCP Python SDK (`mcp`, 2.x). It already includes PyJWT, which checks Cloudflare's token. No other new dependency. |

The owner chose internet access before cutover, against the recommendation to wait. Section 7 lists the controls that make this acceptable, and section 10 lists what the owner sets up.

## 3. Architecture

```
 Claude (Desktop / web / phone)
        │  HTTPS, from Anthropic's cloud
        ▼
 Cloudflare Access (sign-in, policies) ── optional: MCP server portal
        │
 LopezHome tunnel (already running) ──► NAS 192.168.0.33:8001 ── mcp service (new container)
                                                                   │  same code and database as the app
                                                                   ▼
                                                                SQLite (data/dnb.sqlite3)
 books.lopezhome.casa (Access: owner only) ──► NAS :8000 ── app (unchanged)
```

- **A new `mcp` service** in docker-compose runs `manage.py serve_mcp`: an ASGI app (the SDK's `MCPServer` with Streamable HTTP, served by uvicorn, which the SDK already brings). It imports Django and calls the same services the screens use. It doesn't call the web app over HTTP.
- **The `mcp` service** is published on the NAS at port 8001 (bound to `0.0.0.0` in the container) and reached only through the tunnel and its Access application. It serves the MCP endpoint (`/mcp`) over Streamable HTTP, stateless with JSON responses, which Cloudflare's portal accepts. It accepts only the hosts in `DNB_MCP_ALLOWED_HOSTS` (default `mcp.lopezhome.casa`), using the SDK's host check, so the tunnel rule needs no `httpHostHeader` override.
- **The main app** is already reachable at `books.lopezhome.casa` behind the owner-only Access policy. That is separate from this work. Signing in there needs `DNB_CSRF_TRUSTED_ORIGINS` (PR #36).
- **A new Django app, `mcpserver`,** holds the models, the Access token check, the tools, and the screens in Settings.

## 4. Sign-in (Cloudflare Access)

DNB Books does not run its own sign-in for the MCP. Cloudflare Access does, using Managed OAuth on a self-hosted Access application for the MCP hostname. Cloudflare's docs describe this pairing ("Managed OAuth ... allows MCP clients to authenticate users through Access using a standard OAuth 2.0 flow ... The MCP server must validate the Access JWT sent in the `Cf-Access-Jwt-Assertion` header").

1. In Claude, the person adds a custom connector with the URL `https://<mcp domain>/mcp`, or the URL of the owner's MCP server portal if DNB Books is listed there.
2. Claude gets a `401` pointing to Access's OAuth discovery. The person signs in through the owner's identity provider (for example Google Workspace, Microsoft, or a one-time email code). Two-step sign-in is whatever that provider enforces.
3. An Access policy decides who may connect (for example, only the company's email addresses).
4. Every request then reaches the `mcp` service with `Cf-Access-Jwt-Assertion`. The service:
   - checks the signature against the team's public keys (`https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`, cached), and the issuer, the audience tag (`DNB_CF_ACCESS_AUD`), and the expiry. A request without a valid token gets `401`, even if it reached the service some other way;
   - finds the DNB Books user whose email matches the token's `email` claim, case-insensitively. No match, an inactive user, or MCP access turned off for that user gets: "This Cloudflare login (name@example.com) isn't linked to a DNB Books user. Ask an admin to add it in Settings → Users and roles."
5. The person's app role applies to every tool.

**Settings → Claude connections** (admin): an on/off switch for the whole MCP service, an "MCP access" switch per user (off by default), and the call log (section 7). Signing everyone out is done in Cloudflare (Access, revoke sessions). Turning off a user's "Can sign in" or "MCP access" stops their requests at once.

**If DNB Books is added to an MCP server portal:** the portal has its own Access login. Cloudflare's docs warn that "independent MFA ... will not be enforced for MCP servers that are authorized through an MCP portal" and that blocked users "can still connect to the server ... by using its direct URL". So the DNB Books server keeps its own Access application and policy, and two-step sign-in is enforced at the identity provider, not as an Access-only MFA rule.

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
| The books are on the internet | The `mcp` service sits behind its own Access application (owner-only policy to start). DNB Books passwords are never entered for the MCP. |
| Someone reaching the service without Access | The service rejects any request without a valid, unexpired Access token for its audience tag, so a misconfigured route still fails closed. |
| Sign-in strength | Owned by the identity provider: turn on two-step sign-in there. An Access policy limits who can connect. |
| A stolen session | Revoke sessions in Cloudflare Access, the per-user "MCP access" switch, and the service on/off switch. Access session length is set in Cloudflare (recommend 24 hours). |
| Who is who | Identity comes only from the verified token's email, matched to a DNB Books user that an admin has enabled for MCP. |
| A wrong posting from chat | Preview and confirm on every write; no voids through the MCP; closed months enforced; every write recorded in the audit log under the person's name. |
| Unclear activity | A `McpCall` log: user, tool, time, success or error, and the document created. Shown in Settings → Claude connections. Tool arguments are logged with tax IDs removed. |

## 8. Ledger rules

The MCP adds no new posting paths. Every write calls the existing services, which go through `ledger/posting.py`. Reports come from the existing report functions. Money is Decimal throughout.

## 9. Testing

- Access token check: a valid token maps to the right user; a wrong signature, audience, issuer, or an expired token is refused; an unknown email, inactive user, or user with MCP access off gets the plain message; the on/off switch. Tests sign tokens with a test key in place of Cloudflare's.
- Every tool: works for Admin; Standard gets the role message where the app would refuse; unknown-record messages.
- Preview and confirm: nothing posts on preview; confirm posts exactly the preview; expired, reused, and other-user confirmations are refused; a month closed between preview and confirm blocks it.
- Audit: writes record the person as the actor.
- End to end: the SDK's client against the ASGI app in-process (no network), with a test Access token. It lists tools, runs a report, and previews and confirms an invoice.
- Screenshots: Settings → Claude connections, and the per-user MCP access switch.

## 10. What the owner sets up

The Cloudflare side matches the handoff from the owner's Cloudflare session (tunnel `LopezHome`, wildcard Access app "Home services (wildcard)" with the "Owner only" policy).

1. **Tunnel:** on LopezHome, add the public hostname `mcp.lopezhome.casa` → `http://192.168.0.33:8001`, with a proxied CNAME `mcp` to the tunnel. No `httpHostHeader` override.
2. **Access:** add a dedicated self-hosted application for `mcp.lopezhome.casa` with the "Owner only" policy and **Managed OAuth** turned on (Advanced settings). It has to take precedence over the wildcard app, whose browser login MCP clients can't complete. This is untested in this account, so check it before step 5. Copy its **Application Audience (AUD) tag**.
3. **`.env` on the NAS:** `DNB_CF_ACCESS_AUD=<AUD tag>`, `DNB_CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com`, `DNB_MCP_ALLOWED_HOSTS=mcp.lopezhome.casa`. Then `docker compose up -d --build` starts the `mcp` service on port 8001.
4. **DNB Books:** in Settings → Users and roles, make sure your user's email is the one in the Owner only policy, and turn on **MCP access**.
5. **Claude:** add a custom connector `https://mcp.lopezhome.casa/mcp` in the Claude app, and an HTTP server with the same URL in Claude Code. Sign in through Cloudflare when asked.
6. **Optional portal:** in Zero Trust → Access controls → MCP Portals, add the server by its full URL (`https://mcp.lopezhome.casa/mcp`), give the server and the portal an Access policy, and connect Claude to the portal URL instead. Whether the Claude app connects cleanly through a portal here is untested; connect directly first.

## 11. Out of scope

A local stdio server for Claude Desktop (the remote connector covers Desktop too); a sign-in system inside DNB Books (Cloudflare Access does it); MCP access to the items listed in section 5 as not available; email or notifications.

## 12. Build order

1. `mcpserver` app: models (pending confirmations, call log, per-user MCP access), the Access token check, `serve_mcp`.
2. Read tools.
3. Write tools with preview and confirm.
4. Settings → Claude connections: on/off, per-user MCP access, call log.
5. docker-compose services, `.env.example`, README setup section.
