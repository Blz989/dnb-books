// Auto / Light / Dark toggle. Auto = no data-theme attribute (follows the OS).
(function () {
  var KEY = "dnb-theme";
  var root = document.documentElement;
  function apply(choice) {
    if (choice === "light" || choice === "dark") root.setAttribute("data-theme", choice);
    else root.removeAttribute("data-theme");
    var buttons = document.querySelectorAll("[data-theme-choice]");
    for (var i = 0; i < buttons.length; i++) {
      var b = buttons[i];
      b.setAttribute("aria-pressed", String(b.getAttribute("data-theme-choice") === (choice || "auto")));
    }
  }
  function stored() { try { return localStorage.getItem(KEY) || "auto"; } catch (e) { return "auto"; } }
  document.addEventListener("DOMContentLoaded", function () {
    apply(stored());
    var buttons = document.querySelectorAll("[data-theme-choice]");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener("click", function (ev) {
        var choice = ev.currentTarget.getAttribute("data-theme-choice");
        try { localStorage.setItem(KEY, choice); } catch (e) { /* storage unavailable: Auto next time */ }
        apply(choice);
      });
    }
  });
})();
