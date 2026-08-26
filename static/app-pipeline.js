/* HERMES-REO · Pipeline register (II · /deals) — client-side filter + sort.
 * Vanilla JS, instant, no round-trip. State: { county, stage, query, sortKey, sortDir }.
 * Loaded with <script src="/static/app-pipeline.js" defer>. Deterministic:
 * ties break by parcel id, never by array luck or randomness. */
(function () {
  "use strict";

  var STAGE_ORDER = { inbox: 0, enriched: 1, valued: 2, priced: 3 };
  var NUMERIC_KEYS = { sqft: true, cad: true, debt: true, eq: true, ltv: true, mao: true };

  function cellValue(row, key) {
    if (key === "stage") {
      var rank = STAGE_ORDER[row.getAttribute("data-stage")];
      return typeof rank === "number" ? rank : -1;
    }
    if (NUMERIC_KEYS[key]) {
      var n = parseFloat(row.getAttribute("data-" + key));
      return isNaN(n) ? -1 : n;
    }
    var s = row.getAttribute("data-" + key);
    return s == null ? "" : String(s).toLowerCase();
  }

  function init() {
    var table = document.getElementById("registerTable");
    if (!table) return;
    var tbody = document.getElementById("registerBody") || table.tBodies[0];
    var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr.reg-row"));
    var counter = document.getElementById("shownCount");
    var emptyRow = document.getElementById("emptyRow");
    var queryInput = document.getElementById("pipeQuery");

    var state = { county: "All", stage: "All", q: "", sortKey: "", sortDir: 1 };

    /* — segmented filters (county / stage) — */
    Array.prototype.forEach.call(document.querySelectorAll(".segrow"), function (group) {
      var kind = group.getAttribute("data-filter-group"); // "county" | "stage"
      group.addEventListener("click", function (ev) {
        var btn = ev.target && ev.target.closest ? ev.target.closest("button.segbtn") : null;
        if (!btn || !group.contains(btn)) return;
        Array.prototype.forEach.call(group.querySelectorAll("button.segbtn"), function (b) {
          var on = b === btn;
          b.classList.toggle("active", on);
          b.setAttribute("aria-pressed", on ? "true" : "false");
        });
        state[kind] = btn.getAttribute(kind === "county" ? "data-county" : "data-stage");
        apply();
      });
    });

    /* — text filter (address / owner / parcel / city) — */
    if (queryInput) {
      queryInput.addEventListener("input", function () {
        state.q = queryInput.value.trim().toLowerCase();
        apply();
      });
    }

    /* — column sort: toggle asc/desc, ↑/↓ indicator on the active header.
       Headers are tabindex=0, so Enter/Space sorts them too. — */
    var headers = Array.prototype.slice.call(table.querySelectorAll("th.sortable"));

    function sortBy(key) {
      state.sortDir = state.sortKey === key ? -state.sortDir : 1;
      state.sortKey = key;
      Array.prototype.forEach.call(headers, function (h) {
        var active = h.getAttribute("data-sort-key") === state.sortKey;
        var mark = h.querySelector(".sort-mark");
        h.removeAttribute("aria-sort");
        if (mark) mark.textContent = "";
        if (active && mark) {
          mark.textContent = state.sortDir > 0 ? "↑" : "↓";
          h.setAttribute("aria-sort", state.sortDir > 0 ? "ascending" : "descending");
        }
      });
      apply();
    }

    Array.prototype.forEach.call(headers, function (th) {
      th.addEventListener("click", function () {
        sortBy(th.getAttribute("data-sort-key"));
      });
      th.addEventListener("keydown", function (ev) {
        if (ev.key !== "Enter" && ev.key !== " ") return;
        ev.preventDefault();
        sortBy(th.getAttribute("data-sort-key"));
      });
    });

    function apply() {
      var visible = 0;

      var ordered = rows.slice();
      if (state.sortKey) {
        ordered.sort(function (a, b) {
          var ka = cellValue(a, state.sortKey);
          var kb = cellValue(b, state.sortKey);
          var cmp;
          if (typeof ka === "number" && typeof kb === "number") {
            cmp = ka - kb;
          } else {
            cmp = ka < kb ? -1 : ka > kb ? 1 : 0;
          }
          if (cmp === 0) { // deterministic tie-break by parcel id
            var pa = a.getAttribute("data-parcel") || "";
            var pb = b.getAttribute("data-parcel") || "";
            cmp = pa < pb ? -1 : pa > pb ? 1 : 0;
          }
          return cmp * state.sortDir;
        });
      }

      Array.prototype.forEach.call(ordered, function (row) {
        var okCounty = state.county === "All" ||
          row.getAttribute("data-county") === state.county;
        var okStage = state.stage === "All" ||
          row.getAttribute("data-stage") === state.stage.toLowerCase();
        var okQuery = !state.q ||
          (row.getAttribute("data-search") || "").indexOf(state.q) !== -1;
        var show = okCounty && okStage && okQuery;
        row.hidden = !show;
        if (show) visible++;
        tbody.appendChild(row); // settle into sorted order
      });

      if (counter) counter.textContent = visible + " of " + rows.length + " shown";
      if (emptyRow) {
        emptyRow.hidden = visible !== 0;
        tbody.appendChild(emptyRow); // keep the placeholder last in the DOM
      }
    }

    /* — row click → dossier (address anchor handles itself; Enter/Space too) — */
    function openRow(row) {
      if (row && row.hasAttribute("data-href")) {
        window.location.assign(row.getAttribute("data-href"));
      }
    }
    tbody.addEventListener("click", function (ev) {
      if (!ev.target || !ev.target.closest) return;
      if (ev.target.closest("a")) return; // let the address link navigate
      openRow(ev.target.closest("tr.reg-row"));
    });
    tbody.addEventListener("keydown", function (ev) {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      if (!ev.target || !ev.target.closest) return;
      if (ev.target.closest("a")) return;
      ev.preventDefault();
      openRow(ev.target.closest("tr.reg-row"));
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
