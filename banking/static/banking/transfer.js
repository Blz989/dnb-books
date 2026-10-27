// Show the principal and interest fields only for loan payments.
(function () {
  var form = document.getElementById("transfer-form");
  if (!form) return;
  var kind = form.querySelector("[name=kind]");
  var rows = form.querySelectorAll(".loan-only");
  function sync() {
    var loan = kind.value === form.dataset.loan;
    rows.forEach(function (row) { row.hidden = !loan; });
  }
  kind.addEventListener("change", sync);
  sync();
})();
