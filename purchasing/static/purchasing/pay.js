// Pay bills, single payment, and apply screens: live totals, "Pay in full" and "Apply oldest first" without a
// round trip, and the paid-from list and check numbers following the chosen method.
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
  var total = document.getElementById("apply-total");
  var unapplied = document.getElementById("unapplied");
  function update() {
    var applied = 0;
    boxes.forEach(function (b) { applied += cents(b.value); });
    if (total) total.textContent = money(applied);
    if (unapplied) {
      var available = amount ? cents(amount.value) : cents(unapplied.dataset.available || 0);
      var left = available - applied;
      unapplied.textContent = money(left);
      unapplied.classList.toggle("neg", left < 0);
    }
  }
  boxes.forEach(function (b) { b.addEventListener("input", update); });
  if (amount) amount.addEventListener("input", update);

  var full = document.getElementById("pay-in-full");
  if (full) {
    full.addEventListener("click", function (event) {
      event.preventDefault();
      boxes.forEach(function (b) { b.value = (cents(b.dataset.balance) / 100).toFixed(2); });
      update();
    });
  }

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
  var from = document.getElementById("id_paid_from");
  if (method && from) {
    var sync = function () {
      var wanted = method.value === "CARD" ? "CREDIT_CARD" : "BANK";
      var keep = from.value;
      Array.prototype.forEach.call(from.options, function (o) {
        var show = !o.value || o.dataset.subtype === wanted;
        o.hidden = !show;
        o.disabled = !show;
        if (!show && o.value === keep) from.value = "";
      });
      document.querySelectorAll(".check-only").forEach(function (el) { el.hidden = method.value !== "CHECK"; });
    };
    method.addEventListener("change", sync);
    sync();
  }
  update();
});
