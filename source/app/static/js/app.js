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
  // Seiten: lange Listen zeigen nur einen Teil der Angebote (Vorgabe 20, Feld „pro Seite“, auf diesem
  // Gerät gemerkt). Ausgeblendete Zeilen laden ihre Bilder erst, wenn ihre Seite drankommt.

  var PAGE_KEY = "ebay-watcher-page-size";
  var DEFAULT_PAGE_SIZE = 20;

  function savedPageSize() {
    try {
      var n = parseInt(localStorage.getItem(PAGE_KEY), 10);
      return n >= 1 && n <= 200 ? n : DEFAULT_PAGE_SIZE;
    } catch (error) {
      console.info("Ebay Watcher: Seitengröße nicht lesbar (" + error.name + ")");
      return DEFAULT_PAGE_SIZE;
    }
  }

  function rememberPageSize(n) {
    try {
      localStorage.setItem(PAGE_KEY, String(n));
    } catch (error) {
      console.info("Ebay Watcher: Seitengröße wird nicht gespeichert (" + error.name + ")");
    }
  }

  var pageSize = savedPageSize();

  function pagerOf(list) {
    var next = list.nextElementSibling;
    return next && next.hasAttribute("data-pager") ? next : null;
  }

  // Seitenzahlen: erste, letzte und die beiden Nachbarn der aktuellen, dazwischen „…“
  function pageNumbers(page, pages) {
    var out = [];
    for (var p = 1; p <= pages; p++) {
      if (p === 1 || p === pages || Math.abs(p - page) <= 1) {
        out.push(p);
      } else if (out[out.length - 1] !== 0) {
        out.push(0);
      }
    }
    return out;
  }

  function pageButton(pager, text, label, target, current) {
    var button = pager.querySelector("template[data-page-button]").content.firstElementChild.cloneNode(true);
    button.textContent = text;
    button.setAttribute("aria-label", label);
    if (current) button.setAttribute("aria-current", "page");
    if (target === null) {
      button.disabled = true;
    } else {
      button.dataset.page = String(target);
    }
    return button;
  }

  function paginate(list, page) {
    var pager = pagerOf(list);
    if (!pager) return;
    var rows = list.querySelectorAll(":scope > li[data-offer]");
    var pages = Math.max(1, Math.ceil(rows.length / pageSize));
    var current = Math.min(Math.max(1, page || Number(list.dataset.page) || 1), pages);
    list.dataset.page = String(current);
    rows.forEach(function (row, i) {
      row.hidden = i < (current - 1) * pageSize || i >= current * pageSize;
    });

    pager.hidden = rows.length <= pageSize;
    pager.querySelector("[data-page-size]").value = String(pageSize);
    var first = (current - 1) * pageSize + 1;
    pager.querySelector("[data-range]").textContent =
      first + "–" + Math.min(current * pageSize, rows.length) + " von " + rows.length;

    var box = pager.querySelector("[data-pages]");
    box.textContent = "";
    box.appendChild(pageButton(pager, "‹", "Vorige Seite", current > 1 ? current - 1 : null, false));
    pageNumbers(current, pages).forEach(function (p) {
      if (p === 0) {
        box.appendChild(pager.querySelector("template[data-page-gap]").content.firstElementChild.cloneNode(true));
      } else {
        box.appendChild(pageButton(pager, String(p), "Seite " + p, p, p === current));
      }
    });
    box.appendChild(pageButton(pager, "›", "Nächste Seite", current < pages ? current + 1 : null, false));
  }

  function bindPagers(root) {
    root.querySelectorAll("ol[data-sortable]").forEach(function (list) {
      var pager = pagerOf(list);
      if (!pager) return;
      pager.querySelector("[data-pages]").addEventListener("click", function (event) {
        var button = event.target.closest("button[data-page]");
        if (!button) return;
        paginate(list, Number(button.dataset.page));
        if (list.getBoundingClientRect().top < 0) list.scrollIntoView();
      });
      pager.querySelector("[data-page-size]").addEventListener("change", function (event) {
        var n = parseInt(event.target.value, 10);
        if (!(n >= 1)) n = DEFAULT_PAGE_SIZE;
        pageSize = Math.min(200, n);
        rememberPageSize(pageSize);
        document.querySelectorAll("ol[data-sortable]").forEach(function (other) { paginate(other, 1); });
      });
      paginate(list, 1);
    });
  }

  bindPagers(document);

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
      paginate(list, 1);
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
        bindPagers(target);
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
            var ol = row.parentElement;
            row.remove();
            paginate(ol);
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
