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
  // Bilder-Galerie: Klick aufs Vorschaubild zeigt alle Bilder des Angebots (PhotoSwipe, photoswipe.com).
  // /galerie/… liefert Adressen und Maße der Bilder aus dem Bild-Cache. PhotoSwipe selbst (js/photoswipe.js,
  // gebündelt aus node_modules) lädt erst beim ersten Klick, die Adresse steht in <body data-photoswipe>.
  // Ohne JavaScript führt das Bild zu eBay.
  // Seitenleiste (includes/gallery_panel.html, kommt fertig mit /galerie/…): beim Öffnen eingeblendet,
  // der Knopf ⓘ blendet sie aus und ein. Ab 64rem Breite steht sie rechts, darunter unten als Schublade.

  var GALLERY_OPTIONS = {
    bgOpacity: 1,
    showHideAnimationType: "fade",
    wheelToZoom: true,
    preload: [1, 2],
    closeTitle: "Schließen (Esc)",
    zoomTitle: "Vergrößern (Z)",
    arrowPrevTitle: "Vorheriges Bild",
    arrowNextTitle: "Nächstes Bild",
    errorMsg: "Dieses Bild lässt sich gerade nicht laden.",
    indexIndicatorSep: " / "
  };
  // Knopf „Bei eBay öffnen“ oben in der Galerie (Pfeil aus dem Kasten, 32er-Raster wie die PhotoSwipe-Symbole)
  var EBAY_ICON = '<path id="pswp__icn-ebay" d="M9 11h6v2h-4v8h8v-4h2v6H9zM17 9h6v6h-2v-2.6l-5.3 5.3-1.4-1.4 5.3-5.3H17z"/>';
  var INFO_ICON = '<path id="pswp__icn-info" fill-rule="evenodd" d="M16 6.5a9.5 9.5 0 1 1 0 19 9.5 9.5 0 0 1 0-19zm0 2a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15zM15 14h2v7h-2zm0-3.5h2v2h-2z"/>';
  var PANEL_WIDTH = 380;                                   // w-95 in gallery_panel.html
  var wideGallery = window.matchMedia("(min-width: 64rem)");   // lg: Leiste rechts statt unten
  var photoSwipe = null;

  function loadPhotoSwipe() {
    if (!photoSwipe) {
      photoSwipe = new window.Promise(function (resolve, reject) {
        var script = document.createElement("script");
        script.src = document.body.dataset.photoswipe;
        script.onload = function () {
          if (window.PhotoSwipe) resolve(window.PhotoSwipe);
          else reject(new Error("PhotoSwipe fehlt"));
        };
        script.onerror = function () {
          photoSwipe = null;
          reject(new Error("PhotoSwipe nicht geladen"));
        };
        document.head.appendChild(script);
      });
    }
    return photoSwipe;
  }

  function loadImages(link) {
    return fetch(link.dataset.gallery, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      });
  }

  // Platz fürs Bild: neben der Seitenleiste bzw. über der Schublade, damit sie nichts verdeckt
  function galleryPadding(pswp, s) {
    if (!s.panel || s.panel.hidden) return { top: 0, bottom: 0, left: 0, right: 0 };
    if (wideGallery.matches) return { top: 60, bottom: 16, left: 16, right: PANEL_WIDTH + 16 };
    // Handy: das Bild endet dort, wo die Schublade gerade beginnt (höchstens bis zur Mindesthöhe oben)
    var root = pswp.element.getBoundingClientRect();
    var sheetTop = Math.max(s.sheet.getBoundingClientRect().top, s.panel.getBoundingClientRect().top);
    return { top: 56, bottom: Math.max(0, root.bottom - sheetTop) + 8, left: 0, right: 0 };
  }

  // Handy: Abstand über der Schublade so hoch, dass anfangs nur ihr Kopf herausschaut
  function measureSheet(s) {
    if (!s.panel || wideGallery.matches) return;
    var peek = s.peek.getBoundingClientRect().bottom - s.sheet.getBoundingClientRect().top + 20;
    var height = s.panel.clientHeight;
    s.spacer.style.height = Math.max(0, height - Math.min(peek, height * 0.6)) + "px";
  }

  function addPanel(pswp, html) {
    var s = { panel: null, sheet: null, spacer: null, peek: null, frame: 0 };
    pswp.options.paddingFn = function () { return galleryPadding(pswp, s); };

    function relayout() {
      measureSheet(s);
      pswp.updateSize(true);
    }
    // Beim Hochschieben das Bild fortlaufend mitverkleinern (höchstens einmal je Bildschirmaufbau)
    function followSheet() {
      if (s.frame || wideGallery.matches) return;
      s.frame = window.requestAnimationFrame(function () {
        s.frame = 0;
        pswp.updateSize(true);
      });
    }

    pswp.on("uiRegister", function () {
      pswp.ui.registerElement({
        name: "info",
        order: 8,
        isButton: true,
        title: "Angaben ein- oder ausblenden",
        ariaLabel: "Angaben zum Angebot ein- oder ausblenden",
        html: { isCustomSVG: true, inner: INFO_ICON, outlineID: "pswp__icn-info" },
        onInit: function (el) { el.setAttribute("aria-pressed", "true"); },
        onClick: function (event, el) {
          s.panel.hidden = !s.panel.hidden;
          el.setAttribute("aria-pressed", String(!s.panel.hidden));
          pswp.element.dataset.panel = s.panel.hidden ? "closed" : "open";
          relayout();
        }
      });
      pswp.ui.registerElement({
        name: "panel",
        appendTo: "root",
        onInit: function (el) {
          el.innerHTML = html;
          s.panel = el.querySelector("[data-gallery-panel]");
          s.sheet = el.querySelector("[data-sheet]");
          s.spacer = el.querySelector("[data-sheet-spacer]");
          s.peek = el.querySelector("[data-sheet-peek]");
          pswp.element.dataset.panel = "open";
          if (el.isConnected) measureSheet(s);
          // Das Mausrad scrollt in der Leiste den Text, statt das Bild zu zoomen
          el.addEventListener("wheel", function (event) { event.stopPropagation(); }, { passive: true });
          s.panel.addEventListener("scroll", followSheet, { passive: true });
          // Griff: ganz hochschieben bzw. zurück zur großen Galerie
          el.querySelector("[data-sheet-handle]").addEventListener("click", function () {
            var open = s.spacer.offsetHeight;
            s.panel.scrollTo({ top: s.panel.scrollTop < open / 2 ? open : 0, behavior: "smooth" });
          });
          tick();
        }
      });
    });
    // Den Kopf der Schublade messen, sobald sie im Bild ist, und nach dem Einblenden noch einmal
    // (während der Öffnen-Animation passt PhotoSwipe die Bildgröße nicht an)
    pswp.on("afterInit", function () { window.requestAnimationFrame(relayout); });
    pswp.on("openingAnimationEnd", relayout);
    wideGallery.addEventListener("change", relayout);
    window.addEventListener("resize", relayout);
    pswp.on("destroy", function () {
      wideGallery.removeEventListener("change", relayout);
      window.removeEventListener("resize", relayout);
    });
  }

  function openGallery(link) {
    var thumb = link.querySelector("img");
    link.setAttribute("aria-busy", "true");
    window.Promise.all([loadPhotoSwipe(), loadImages(link)])
      .then(function (results) {
        var PhotoSwipe = results[0];
        var items = results[1].items || [];
        if (!items.length && !results[1].panel) throw new Error("keine Bilder");
        if (!items.length) {   // keine Bilder, aber Beschreibung: Galerie trotzdem öffnen
          items = [{ html: '<p class="grid h-full place-items-center px-6 text-center text-ui text-white/70">Zu diesem Angebot liefert eBay gerade keine Bilder.</p>' }];
        } else if (thumb) {
          items[0].msrc = thumb.currentSrc || thumb.src;   // Vorschaubild, bis das große Bild da ist
        }
        var pswp = new PhotoSwipe(Object.assign({ dataSource: items }, GALLERY_OPTIONS));
        if (results[1].panel) addPanel(pswp, results[1].panel);
        pswp.on("uiRegister", function () {
          pswp.ui.registerElement({
            name: "ebay",
            order: 9,
            isButton: true,
            tagName: "a",
            title: "Bei eBay öffnen",
            ariaLabel: "Bei eBay öffnen",
            html: { isCustomSVG: true, inner: EBAY_ICON, outlineID: "pswp__icn-ebay" },
            onInit: function (el) {
              el.href = link.href;
              el.target = "_blank";
              el.rel = "noopener noreferrer";
            }
          });
        });
        pswp.init();
      })
      .catch(function (error) {
        // Ohne Galerie wenigstens das Angebot bei eBay zeigen, wie ohne JavaScript
        console.info("Ebay Watcher: Galerie nicht verfügbar (" + error.message + ")");
        var tab = window.open(link.href, "_blank");
        if (tab) tab.opener = null;
        else window.location.assign(link.href);   // Pop-up gesperrt: dann eben in diesem Tab
      })
      .then(function () { link.removeAttribute("aria-busy"); });
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("a[data-gallery]") : null;
    if (!link || !document.body.dataset.photoswipe) return;
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;   // neuer Tab: eBay
    event.preventDefault();
    if (link.getAttribute("aria-busy") !== "true") openGallery(link);
  });

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
