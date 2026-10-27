// Saves each tick as it happens and keeps the difference current. Without JavaScript the page's own form works.
(function () {
  var form = document.getElementById("ticks-form");
  if (!form) return;
  var csrf = form.querySelector("[name=csrfmiddlewaretoken]").value;
  var diff = document.getElementById("difference");
  var cleared = document.getElementById("cleared-balance");
  var finish = document.getElementById("finish-button");

  function update(data) {
    diff.textContent = data.difference;
    diff.className = "difference " + (data.can_finish ? "ok" : "warn");
    cleared.textContent = data.cleared_balance;
    if (finish) finish.disabled = !(data.can_finish && finish.dataset.admin === "yes");
  }

  form.querySelectorAll("input[type=checkbox]").forEach(function (box) {
    box.addEventListener("change", function () {
      var body = new URLSearchParams({ cleared: box.checked ? "1" : "0" });
      fetch(box.dataset.toggleUrl, {
        method: "POST", credentials: "same-origin", body: body,
        headers: { "X-CSRFToken": csrf, "Content-Type": "application/x-www-form-urlencoded" }
      }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, json: j }; }); })
        .then(function (res) {
          if (!res.ok) { box.checked = !box.checked; window.alert(res.json.error || "That could not be saved."); return; }
          update(res.json);
        })
        .catch(function () { box.checked = !box.checked; window.alert("That could not be saved. Check the connection and try again."); });
    });
  });
})();
