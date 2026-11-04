// Reveal the next hidden row of a lines table (orders, invoices, journal entries).
document.addEventListener("DOMContentLoaded", function () {
  var button = document.getElementById("add-line");
  if (!button) return;
  var table = button.getAttribute("data-table") || "doc-lines";
  var sel = "#" + table + " tr[data-extra][hidden]";
  if (!document.querySelector(sel)) button.disabled = true;
  button.addEventListener("click", function () {
    var next = document.querySelector(sel);
    if (next) next.hidden = false;
    if (!document.querySelector(sel)) button.disabled = true;
  });
});
