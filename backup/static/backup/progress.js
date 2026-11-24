// Keeps the backup progress bar on the home screen current. When the backup ends, the page reloads
// so the card and any warning show the result. Without JavaScript, reload the page to see progress.
(function () {
  var card = document.getElementById("backup-card");
  var box = card && card.querySelector(".backup-progress");
  if (!box) return;
  var bar = box.querySelector(".bar");
  var fill = bar.querySelector("span");
  var pct = box.querySelector("[data-pct]");
  var label = box.querySelector("[data-label]");
  var detail = box.querySelector("[data-detail]");
  var failures = 0;

  function show(data) {
    fill.style.width = data.progress + "%";
    bar.setAttribute("aria-valuenow", data.progress);
    pct.textContent = data.progress + "%";
    label.textContent = data.label;
    detail.textContent = data.detail;
  }

  function poll() {
    fetch(card.dataset.statusUrl, { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        failures = 0;
        if (!data.active) { window.location.reload(); return; }
        show(data);
        setTimeout(poll, 1000);
      })
      .catch(function () {
        failures += 1;
        if (failures === 5) detail.textContent = "Can't reach the app to check progress. Still trying.";
        setTimeout(poll, 3000);
      });
  }

  setTimeout(poll, 1000);
})();
