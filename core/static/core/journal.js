// Running debit and credit totals on the journal entry form. Display only; the server checks the balance.
document.addEventListener("DOMContentLoaded", function () {
  var table = document.getElementById("je-lines");
  if (!table) return;
  function cents(v) { var n = parseFloat(String(v).replace(/,/g, "")); return isFinite(n) ? Math.round(n * 100) : 0; }
  function fmt(c) { return (c / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function update() {
    var d = 0, c = 0;
    table.querySelectorAll("input.amt").forEach(function (i) {
      if (i.getAttribute("data-side") === "debit") d += cents(i.value); else c += cents(i.value);
    });
    table.querySelector("[data-total=debit]").textContent = fmt(d);
    table.querySelector("[data-total=credit]").textContent = fmt(c);
    var diff = table.querySelector("[data-total=difference]");
    diff.textContent = fmt(Math.abs(d - c));
    diff.className = "num " + (d === c && d > 0 ? "ok-text" : (d !== c ? "neg" : ""));
  }
  table.addEventListener("input", update);
  update();
});
