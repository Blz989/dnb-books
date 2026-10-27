// Receive payment and deposit screens: live totals, and "Apply oldest first" without a round trip.
document.addEventListener("DOMContentLoaded", function () {
  function cents(text) {
    var n = parseFloat(String(text).replace(/,/g, ""));
    return isNaN(n) ? 0 : Math.round(n * 100);
  }
  function money(c) {
    return (c / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  var amount = document.getElementById("id_amount");
  var boxes = document.querySelectorAll("input[data-balance]");
  var unapplied = document.getElementById("unapplied");
  var total = document.getElementById("apply-total");
  function update() {
    var applied = 0;
    boxes.forEach(function (b) { applied += cents(b.value); });
    if (total) total.textContent = money(applied);
    if (unapplied) {
      var left = (amount ? cents(amount.value) : cents(unapplied.dataset.available || 0)) - applied;
      unapplied.textContent = money(left);
      unapplied.classList.toggle("neg", left < 0);
    }
  }
  boxes.forEach(function (b) { b.addEventListener("input", update); });
  if (amount) amount.addEventListener("input", update);

  var fill = document.getElementById("fill-oldest");
  if (fill) {
    fill.addEventListener("click", function (event) {
      event.preventDefault();
      var left = amount ? cents(amount.value) : cents(fill.dataset.available || 0);
      boxes.forEach(function (b) {
        var take = Math.min(left, cents(b.dataset.balance));
        b.value = take > 0 ? (take / 100).toFixed(2) : "";
        left -= take;
      });
      update();
    });
  }

  var method = document.getElementById("id_method");
  var bankRow = document.getElementById("bank-row");
  if (method && bankRow) {
    var sync = function () { bankRow.hidden = !(method.value === "ACH" || method.value === "WIRE"); };
    method.addEventListener("change", sync);
    sync();
  }

  var picks = document.querySelectorAll("input[name=payments]");
  var depositTotal = document.getElementById("deposit-total");
  if (picks.length && depositTotal) {
    var sum = function () {
      var c = 0;
      picks.forEach(function (p) { if (p.checked) c += cents(p.dataset.amount); });
      depositTotal.textContent = money(c);
    };
    picks.forEach(function (p) { p.addEventListener("change", sum); });
    sum();
  }
  update();
});
