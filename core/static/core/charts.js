// Hover tooltips for the server-drawn SVG charts. Each hit area carries its text in data-tip.
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-chart]").forEach(function (box) {
    var tip = box.querySelector(".tip");
    if (!tip) return;
    box.querySelectorAll(".hit").forEach(function (hit) {
      var t = hit.querySelector("title");
      if (t) t.remove(); // the custom tooltip replaces the browser's
      hit.addEventListener("mouseenter", function () {
        tip.innerHTML = "";
        hit.getAttribute("data-tip").split(" · ").forEach(function (part, i) {
          var row = document.createElement("div");
          row.textContent = part;
          if (i === 0) row.className = "tip-h";
          tip.appendChild(row);
        });
        tip.hidden = false;
        box.querySelectorAll(".hit.on").forEach(function (h) { h.classList.remove("on"); });
        hit.classList.add("on");
      });
      hit.addEventListener("mousemove", function (ev) {
        var r = box.getBoundingClientRect();
        var x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 10;
        if (x + tip.offsetWidth > r.width) x = ev.clientX - r.left - tip.offsetWidth - 14;
        tip.style.left = x + "px"; tip.style.top = y + "px";
      });
      hit.addEventListener("mouseleave", function () { tip.hidden = true; hit.classList.remove("on"); });
    });
  });
});
