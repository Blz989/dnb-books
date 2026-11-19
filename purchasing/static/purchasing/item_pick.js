// Item picker for document line tables. Type a name or a SKU; suggestions come from the item search endpoint
// (a few at a time, so it stays fast with thousands of items). Picking fills "Item name (SKU)".
document.addEventListener("DOMContentLoaded", function () {
  var table = document.querySelector("table[data-search-url]");
  var list = document.getElementById("item-suggest");
  if (!table || !list) return;
  var url = table.dataset.searchUrl, active = null, timer = null, shown = [], index = -1, seq = 0;

  function close() { list.hidden = true; list.innerHTML = ""; shown = []; index = -1; }

  function place() {
    var r = active.getBoundingClientRect();
    list.style.left = (r.left + window.scrollX) + "px";
    list.style.top = (r.bottom + window.scrollY) + "px";
    list.style.minWidth = r.width + "px";
  }

  function highlight(i) {
    index = i;
    Array.prototype.forEach.call(list.children, function (li, n) { li.classList.toggle("on", n === i); });
  }

  function pick(row) {
    active.value = row.label;
    var description = active.closest("tr").querySelector("input[name$='-description']");
    if (description) description.placeholder = row.name;
    var price = active.closest("tr").querySelector("input[name$='-price']");
    if (price && !price.value) price.placeholder = "Cost " + row.cost;
    close();
  }

  function render(rows) {
    shown = rows;
    list.innerHTML = "";
    if (!rows.length) {
      var none = document.createElement("li");
      none.className = "none";
      none.textContent = "No item matches.";
      list.appendChild(none);
    }
    rows.forEach(function (row, n) {
      var li = document.createElement("li");
      li.setAttribute("role", "option");
      var name = document.createElement("span");
      name.textContent = row.name;
      var sku = document.createElement("small");
      sku.textContent = row.sku;
      li.appendChild(name);
      li.appendChild(sku);
      li.addEventListener("mousedown", function (e) { e.preventDefault(); pick(row); });
      list.appendChild(li);
    });
    index = -1;
    place();
    list.hidden = false;
  }

  function search() {
    var mine = ++seq;
    fetch(url + "?q=" + encodeURIComponent(active.value.trim()), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) { if (mine === seq && active) render(data.results); })
      .catch(function () {});
  }

  table.addEventListener("focusin", function (e) {
    if (!e.target.classList.contains("item-pick")) return;
    active = e.target;
    active.select();
    search();
  });
  table.addEventListener("input", function (e) {
    if (!e.target.classList.contains("item-pick")) return;
    active = e.target;
    clearTimeout(timer);
    timer = setTimeout(search, 120);
  });
  table.addEventListener("keydown", function (e) {
    if (!e.target.classList.contains("item-pick") || list.hidden) return;
    if (e.key === "ArrowDown") { e.preventDefault(); highlight(Math.min(index + 1, shown.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); highlight(Math.max(index - 1, 0)); }
    else if (e.key === "Enter" && index >= 0) { e.preventDefault(); pick(shown[index]); }
    else if (e.key === "Escape") { close(); }
  });
  table.addEventListener("focusout", function (e) {
    if (e.target.classList.contains("item-pick")) setTimeout(close, 100);
  });
  window.addEventListener("resize", function () { if (!list.hidden && active) place(); });
});
