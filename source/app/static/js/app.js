/*
 * Ebay Watcher – kleine Helfer im Browser. Alles funktioniert auch ohne JavaScript.
 *
 * Das ist die QUELLE. CodeKit verkleinert sie nach build/app/static/js/app-min.js.
 * Tailwind durchsucht diese Datei ebenfalls nach Klassen und Attributen (siehe app.css).
 */
(function () {
  "use strict";

  // ------------------------------------------------------------------
  // Übersicht: Karten oder Liste (wird auf diesem Gerät gemerkt)
  // Schaltet das Attribut data-view um; die Klassen view-cards:… und view-list:… reagieren darauf.

  var collection = document.querySelector("[data-collection]");
  var viewButtons = document.querySelectorAll("button[data-set-view]");
  var VIEW_KEY = "ebay-watcher-view";

  // Im privaten Surfen kann der Browser den Speicher sperren. Dann gilt einfach die Standardansicht,
  // und in der Browser-Konsole steht ein kurzer Hinweis.
  function rememberView(view) {
    try {
      localStorage.setItem(VIEW_KEY, view);
    } catch (error) {
      console.info("Ebay Watcher: Ansicht wird nicht gespeichert (" + error.name + ")");
    }
  }

  function savedView() {
    try {
      return localStorage.getItem(VIEW_KEY);
    } catch (error) {
      console.info("Ebay Watcher: gespeicherte Ansicht nicht lesbar (" + error.name + ")");
      return null;
    }
  }

  function setView(view) {
    if (!collection) return;
    collection.setAttribute("data-view", view);
    viewButtons.forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.setView === view));
    });
    rememberView(view);
  }

  if (collection) {
    var saved = savedView();
    if (saved === "list" || saved === "cards") setView(saved);
    viewButtons.forEach(function (b) {
      b.addEventListener("click", function () { setView(b.dataset.setView); });
    });
  }

  // ------------------------------------------------------------------
  // Restlaufzeiten aktuell halten. In der letzten Stunde bekommt die Zeit
  // das Attribut data-soon, das die Klassen data-soon:… hervorheben.

  function format(left) {
    if (left <= 0) return "beendet";
    if (left < 60) return "unter 1 Min";
    var minutes = Math.floor(left / 60);
    var hours = Math.floor(minutes / 60);
    minutes = minutes % 60;
    var days = Math.floor(hours / 24);
    hours = hours % 24;
    if (days >= 1) return days + (days === 1 ? " Tag " : " Tage ") + hours + " Std";
    if (hours >= 1) return hours + " Std " + minutes + " Min";
    return minutes + " Min";
  }

  function tick() {
    var now = Date.now() / 1000;
    document.querySelectorAll("[data-end]").forEach(function (el) {
      var left = Math.floor(Number(el.dataset.end) - now);
      el.textContent = format(left);
      el.toggleAttribute("data-soon", left > 0 && left < 3600);
    });
  }

  tick();
  setInterval(tick, 15000);

  // ------------------------------------------------------------------
  // Detailseite: Angebote sortieren (wird auf diesem Gerät gemerkt).
  // Jede Zeile trägt data-sort-end, data-sort-price, data-sort-new und data-sort-distance.

  var SORT_KEY = "ebay-watcher-sort";
  var sortControl = document.querySelector("[data-sort-control]");
  var sortButtons = document.querySelectorAll("button[data-sort]");

  function rememberSort(key) {
    try {
      localStorage.setItem(SORT_KEY, key);
    } catch (error) {
      console.info("Ebay Watcher: Sortierung wird nicht gespeichert (" + error.name + ")");
    }
  }

  function savedSort() {
    try {
      return localStorage.getItem(SORT_KEY);
    } catch (error) {
      console.info("Ebay Watcher: gespeicherte Sortierung nicht lesbar (" + error.name + ")");
      return null;
    }
  }

  // Leere Werte (z. B. keine Endzeit) kommen immer ans Ende
  function sortValue(el, name, descending) {
    var raw = el.getAttribute("data-sort-" + name);
    if (raw === null || raw === "") return Infinity;
    var value = Number(raw);
    return descending ? -value : value;
  }

  var SORTS = {
    end: [["end", false], ["price", false]],
    price: [["price", false], ["end", false]],
    "new": [["new", true], ["end", false]],
    distance: [["distance", false], ["end", false]]
  };

  function compareBy(rules) {
    return function (a, b) {
      for (var i = 0; i < rules.length; i++) {
        var x = sortValue(a, rules[i][0], rules[i][1]);
        var y = sortValue(b, rules[i][0], rules[i][1]);
        if (x !== y) return x < y ? -1 : 1;
      }
      return 0;
    };
  }

  function currentSort() {
    var pressed = document.querySelector('button[data-sort][aria-pressed="true"]');
    return pressed ? pressed.dataset.sort : "end";
  }

  function applySort(key, root) {
    var rules = SORTS[key] || SORTS.end;
    (root || document).querySelectorAll("ol[data-sortable]").forEach(function (list) {
      var rows = Array.prototype.slice.call(list.querySelectorAll(":scope > li[data-offer]"));
      rows.sort(compareBy(rules)).forEach(function (row) { list.appendChild(row); });
    });
  }

  function setSort(key) {
    sortButtons.forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.sort === key)); });
    applySort(key);
    rememberSort(key);
  }

  if (sortControl) {
    sortControl.hidden = false;
    var wanted = savedSort();
    var available = Array.prototype.some.call(sortButtons, function (b) { return b.dataset.sort === wanted; });
    if (wanted && wanted !== "end" && available) setSort(wanted);
    sortButtons.forEach(function (b) {
      b.addEventListener("click", function () { setSort(b.dataset.sort); });
    });
  }

  // ------------------------------------------------------------------
  // Eingeklappte Listen („Mit Versand über dem Limit“, „Ausgeblendet“) erst beim Aufklappen laden

  function loadFold(fold) {
    if (fold.hasAttribute("data-loaded")) return;
    var target = fold.querySelector("[data-lazy-target]");
    fold.setAttribute("data-loaded", "");
    fetch(fold.dataset.lazy, { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.text();
      })
      .then(function (html) {
        target.innerHTML = html;
        applySort(currentSort(), target);
        bindHideForms(target);
        tick();
      })
      .catch(function (error) {
        fold.removeAttribute("data-loaded");
        console.info("Ebay Watcher: Liste konnte nicht geladen werden (" + error.message + ")");
      });
  }

  document.querySelectorAll("details[data-lazy]").forEach(function (fold) {
    fold.addEventListener("toggle", function () { if (fold.open) loadFold(fold); });
  });

  // ------------------------------------------------------------------
  // Ausblenden ohne Neuladen der Seite: Zeile entfernen und Zähler anpassen.
  // Ohne JavaScript schickt das Formular ganz normal ab.

  function changeCount(name, delta) {
    var el = document.querySelector('[data-count="' + name + '"]');
    if (el) el.textContent = String(Math.max(0, Number(el.textContent) + delta));
  }

  function bindHideForms(root) {
    root.querySelectorAll("form[data-hide]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var row = form.closest("li");
        fetch(form.action, { method: "POST", body: new FormData(form), credentials: "same-origin" })
          .then(function (response) {
            if (!response.ok) throw new Error("HTTP " + response.status);
            var list = row.closest("[data-list]");
            row.remove();
            if (list) changeCount(list.dataset.list, -1);
            var hiddenFold = document.getElementById("ausgeblendet");
            if (hiddenFold) {
              hiddenFold.hidden = false;
              changeCount("hidden", 1);
              hiddenFold.removeAttribute("data-loaded");
              if (hiddenFold.open) loadFold(hiddenFold);
            }
          })
          .catch(function () { form.submit(); });
      });
    });
  }

  bindHideForms(document);

  // ------------------------------------------------------------------
  // Sicherheitsabfrage vor dem Löschen

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  // ------------------------------------------------------------------
  // Gesuch-Formular: Erinnerung nur zeigen, wenn Auktionen dabei sind

  var formatInputs = document.querySelectorAll('input[name="format"]');

  function syncFormat() {
    var checked = document.querySelector('input[name="format"]:checked');
    var value = checked ? checked.value : "auction";
    document.querySelectorAll("[data-when-format]").forEach(function (el) {
      el.hidden = el.dataset.whenFormat.split(" ").indexOf(value) === -1;
    });
  }

  formatInputs.forEach(function (i) { i.addEventListener("change", syncFormat); });
  if (formatInputs.length) syncFormat();
})();
