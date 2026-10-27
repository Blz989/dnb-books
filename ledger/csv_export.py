import csv

from django.http import HttpResponse


def amt(value) -> str:
    """Plain number with 2 decimals, no thousands separator."""
    return f"{value:.2f}"


def csv_response(filename: str, header: list[str], rows: list[list]) -> HttpResponse:
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(header)
    writer.writerows(rows)
    return response


def trial_balance_csv(tb):
    rows = [[r.account.number, r.account.name, amt(r.debit), amt(r.credit)] for r in tb.rows]
    rows.append(["", "Total", amt(tb.total_debit), amt(tb.total_credit)])
    return csv_response(
        f"trial-balance-{tb.as_of.isoformat()}.csv", ["Account", "Name", "Debit", "Credit"], rows
    )


def general_ledger_csv(gl):
    rows = [["", "", "", "Opening balance", "", "", amt(gl.opening_balance)]]
    rows += [
        [l.date.isoformat(), l.entry_number, l.reference, l.description, amt(l.debit), amt(l.credit), amt(l.balance)]
        for l in gl.lines
    ]
    rows.append(["", "", "", "Closing balance", "", "", amt(gl.closing_balance)])
    return csv_response(
        f"general-ledger-{gl.account.number}-{gl.start.isoformat()}_{gl.end.isoformat()}.csv",
        ["Date", "Entry", "Reference", "Description", "Debit", "Credit", "Balance"],
        rows,
    )


def _section_rows(title, rows):
    return [[title, r.account.number, r.account.name, amt(r.amount)] for r in rows]


def profit_and_loss_csv(p):
    rows = (
        _section_rows("Income", p.income)
        + _section_rows("Cost of goods sold", p.cost_of_goods_sold)
        + [["Gross profit", "", "", amt(p.gross_profit)]]
        + _section_rows("Operating expenses", p.operating_expenses)
        + _section_rows("Other income", p.other_income)
        + _section_rows("Other expenses", p.other_expenses)
        + [["Net income", "", "", amt(p.net_income)]]
    )
    return csv_response(
        f"profit-and-loss-{p.start.isoformat()}_{p.end.isoformat()}.csv",
        ["Section", "Account", "Name", "Amount"], rows,
    )


def balance_sheet_csv(bs):
    rows = (
        _section_rows("Assets", bs.assets)
        + [["Total assets", "", "", amt(bs.total_assets)]]
        + _section_rows("Liabilities", bs.liabilities)
        + _section_rows("Equity", bs.equity)
        + [["Retained earnings", "", "", amt(bs.retained_earnings)]]
        + [["Current year earnings", "", "", amt(bs.current_year_earnings)]]
        + [["Total liabilities and equity", "", "", amt(bs.total_liabilities_and_equity)]]
    )
    return csv_response(
        f"balance-sheet-{bs.as_of.isoformat()}.csv", ["Section", "Account", "Name", "Amount"], rows
    )
