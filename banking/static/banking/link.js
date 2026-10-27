// Plaid Link for DNB Books. Plaid's own script (loaded on the link page only) provides window.Plaid.
(function () {
  var button = document.getElementById("link-button");
  if (!button) return;
  var errorBox = document.getElementById("link-error");

  function show(message) {
    errorBox.textContent = message || "Plaid could not complete the request. Try again.";
    errorBox.hidden = false;
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": button.dataset.csrf },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json().then(function (json) { return { ok: r.ok, json: json }; }); });
  }

  function go(res) {
    if (!res.ok) { show(res.json.error); return; }
    window.location.href = res.json.redirect;
  }

  button.addEventListener("click", function () {
    errorBox.hidden = true;
    post(button.dataset.tokenUrl).then(function (res) {
      if (!res.ok) { show(res.json.error); return; }
      var handler = window.Plaid.create({
        token: res.json.link_token,
        onSuccess: function (publicToken, metadata) {
          if (button.dataset.mode === "update") {
            post(button.dataset.doneUrl).then(go);
          } else {
            var name = metadata && metadata.institution ? metadata.institution.name : "";
            post(button.dataset.exchangeUrl, { public_token: publicToken, institution_name: name }).then(go);
          }
        },
        onExit: function (err) { if (err) show(err.display_message || err.error_message); }
      });
      handler.open();
    });
  });
})();
