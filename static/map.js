'use strict';
/* =========================================================================
 * HERMES-REO — IV · Map (/map) — vanilla JS, no dependencies.
 * The server renders every marker and list row; this file only holds
 * interaction state { county, selId }: click a circle or a row to select,
 * segmented county filter hides the rest. Instant, no round-trip.
 * ========================================================================= */
(function () {
  const dataEl = document.getElementById('map-data');
  if (!dataEl) return;

  let PTS = [];
  try { PTS = JSON.parse(dataEl.textContent || '[]'); } catch (err) { PTS = []; }
  if (!PTS.length) return;

  const state = { county: 'All', selId: Number(PTS[0].id) };
  const byId = {};
  PTS.forEach((p) => { byId[Number(p.id)] = p; });

  const dots = Array.prototype.slice.call(document.querySelectorAll('.map-dot[data-id]'));
  const rows = Array.prototype.slice.call(document.querySelectorAll('.map-row[data-id]'));
  const shownEl = document.getElementById('map-shown');
  const seg = document.getElementById('map-seg');

  const el = {
    county: document.getElementById('map-sel-county'),
    title: document.getElementById('map-sel-title'),
    cad: document.getElementById('map-sel-cad'),
    loc: document.getElementById('map-sel-loc'),
    parcel: document.getElementById('map-sel-parcel'),
    link: document.getElementById('map-sel-link'),
    nolink: document.getElementById('map-sel-nolink'),
  };

  /* County filter — hide markers + rows outside the chosen county,
     keep the "Mapped leads · n" counter honest. */
  function applyCounty() {
    let shown = 0;
    dots.forEach((c) => {
      c.classList.toggle('map-hidden',
        state.county !== 'All' && c.dataset.county !== state.county);
    });
    rows.forEach((r) => {
      const hide = state.county !== 'All' && r.dataset.county !== state.county;
      r.classList.toggle('map-hidden', hide);
      if (!hide) shown += 1;
    });
    if (shownEl) shownEl.textContent = String(shown);
  }

  function paintSelection() {
    dots.forEach((c) => {
      c.classList.toggle('is-selected', Number(c.dataset.id) === state.selId);
      c.setAttribute('aria-pressed', Number(c.dataset.id) === state.selId ? 'true' : 'false');
    });
    rows.forEach((r) => {
      const on = Number(r.dataset.id) === state.selId;
      r.classList.toggle('is-selected', on);
      r.setAttribute('aria-selected', on ? 'true' : 'false');
    });
  }

  /* Selected-point card — fields come from the server-rendered payload so
     client and markup can never disagree about copy or formatting. */
  function renderCard() {
    const p = byId[state.selId];
    if (!p) return;
    if (el.county) el.county.textContent = p.county || '—';
    if (el.title) el.title.textContent = p.short || p.full || '—';
    if (el.cad) el.cad.textContent = p.cadText || '—';
    if (el.loc) el.loc.textContent = p.loc || '—';
    if (el.parcel) el.parcel.textContent = p.parcel || '—';
    if (el.link && el.nolink) {
      if (p.parcel) {
        el.link.setAttribute('href', '/deals/' + encodeURIComponent(p.parcel));
        el.link.hidden = false;
        el.nolink.hidden = true;
      } else {
        el.link.hidden = true;
        el.nolink.hidden = false;
      }
    }
  }

  function select(id) {
    id = Number(id);
    if (!(id in byId)) return;
    state.selId = id;
    paintSelection();
    renderCard();
  }

  dots.concat(rows).forEach((node) => {
    node.addEventListener('click', () => select(node.dataset.id));
    node.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        select(node.dataset.id);
      }
    });
  });

  if (seg) {
    seg.addEventListener('change', (ev) => {
      if (ev.target && ev.target.name === 'map-county') {
        state.county = ev.target.value;
        applyCounty();
      }
    });
  }

  applyCounty();
  paintSelection();
  renderCard();
})();
