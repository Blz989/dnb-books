// Reveal the next hidden row of the adjustment form.
document.addEventListener("DOMContentLoaded", function () {
  var button = document.getElementById("add-line");
  if (!button) return;
  button.addEventListener("click", function () {
    var next = document.querySelector("#adjust-lines tr[data-extra][hidden]");
    if (next) next.hidden = false;
    if (!document.querySelector("#adjust-lines tr[data-extra][hidden]")) button.disabled = true;
  });
});
