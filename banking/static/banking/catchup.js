// Keep the running total and the Save button in step with the amounts typed on a catch-up form.
(function () {
  var form = document.getElementById("catchup-form");
  if (!form) return;
  var bank = parseFloat(form.dataset.bank);
  var sum = form.querySelector("[data-sum]");
  var box = form.querySelector(".live-total");
  var inputs = form.querySelectorAll("input[name^=amt-]");
  function update() {
    var cents = 0;
    inputs.forEach(function (input) {
      var value = parseFloat(input.value.replace(/,/g, ""));
      if (!isNaN(value)) cents += Math.round(value * 100);
    });
    sum.textContent = (cents / 100).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
    var balanced = cents === Math.round(bank * 100);
    box.classList.toggle("balanced", balanced);
    box.classList.toggle("off", !balanced);
  }
  inputs.forEach(function (input) { input.addEventListener("input", update); });
  update();
})();

// Carry the chosen method through "Show open ..." and "Fill oldest first", which submit the separate pick form.
(function () {
  var select = document.querySelector("#catchup-form select[name=method]");
  var hidden = document.getElementById("pick-method");
  if (!select || !hidden) return;
  select.addEventListener("change", function () { hidden.value = select.value; });
})();
