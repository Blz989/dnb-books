// Reveal the next hidden row of a line table. A button names its table: <button class="add-rows" data-table="doc-lines">.
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("button.add-rows").forEach(function (button) {
    var selector = "#" + button.dataset.table + " tr[data-extra][hidden]";
    if (!document.querySelector(selector)) button.disabled = true;
    button.addEventListener("click", function () {
      var next = document.querySelector(selector);
      if (next) next.hidden = false;
      if (!document.querySelector(selector)) button.disabled = true;
    });
  });
});
