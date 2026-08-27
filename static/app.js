'use strict';
/* =========================================================================
 * HERMES-REO dashboard — plain ES6, no build tooling (per DASHBOARD_PLAN §0,
 * archived at docs/archive/DASHBOARD_PLAN__2026-08-20.md)
 * ========================================================================= */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

function num(v){                       // tolerant numeric coercion
  if (typeof v === 'number') return isFinite(v) ? v : null;
  if (typeof v === 'string' && v.trim() !== ''){
    const n = parseFloat(v.replace(/[$,]/g,''));
    return isNaN(n) ? null : n;
  }
  return null;
}
function fmtMoney(v, compact){
  if (v == null) return '—';
  if (compact){
    if (Math.abs(v) >= 1e9) return '$' + (v/1e9).toFixed(2) + 'B';
    if (Math.abs(v) >= 1e6) return '$' + (v/1e6).toFixed(2) + 'M';
    if (Math.abs(v) >= 1e3) return '$' + (v/1e3).toFixed(0) + 'K';
  }
  return '$' + Math.round(v).toLocaleString('en-US');
}
function shortWord(s, n){ return String(s || '').trim().split(/\s+/)[0] || '—'; }

/* The geojson stores coordinates as *stringified* ProvenanceField reprs
   ("ProvenanceField(value=-96.79, source=…)") rather than raw numbers —
   parse defensively so both shapes work. */
function parseCoord(v){
  if (typeof v === 'number') return isFinite(v) ? v : null;
  if (typeof v === 'string'){
    let m = v.match(/value=(-?\d+(?:\.\d+)?)/);
    if (m) { const x = parseFloat(m[1]); if (isFinite(x)) return x; }
    const n = parseFloat(v);
    return isNaN(n) ? null : n;
  }
  return null;
}

async function fetchJSON(url){
  const r = await fetch(url, {headers:{'Accept':'application/json'}});
  if (!r.ok){
    let detail = '';
    try { detail = (await r.json()).detail || ''; } catch(e){}
    throw new Error(`${url} → HTTP ${r.status}${detail ? ': ' + detail : ''}`);
  }
  return r.json();
}
function showError(id, msg){ const el=$(id); el.textContent='⚠ '+msg; el.style.display=msg?'block':'none'; }

/* ---------------- state ---------------- */
const state = {
  deals: [], summary: null,
  filters:{ q:'', county:'', city:'', tier:'', debtMin:null, debtMax:null },
  sort:{ key:'address', dir:1 },
  page:0, pageSize:50,
  map:{ inited:false, map:null, layer:null, features:[], county:'', tier:'' },
  detail:{ parcel:null, memoLoaded:false },
  market:{ loaded:false, groupby:'zip' },
};

/* ---------------- tabs ---------------- */
function switchView(name){
  document.querySelectorAll('nav button').forEach(b=>
    b.classList.toggle('active', b.dataset.view===name));
  document.querySelectorAll('section.view').forEach(s=>s.classList.remove('active'));
  const sec = $('view-'+name);
  if (sec) sec.classList.add('active');
  if (name==='map') initMap();
  if (name==='market') loadMarket();
}
document.querySelectorAll('nav button').forEach(btn=>{
  btn.addEventListener('click', ()=>switchView(btn.dataset.view));
});

/* ================= OVERVIEW ================= */
function aggregate(deals){
  const tiers={}, cities={}, lenders={}, counties={};
  let debt=0, lienPass=0, lienFail=0, valRej=0, hitl=0;
  for (const d of deals){
    const t=d.blast_tier||'?'; tiers[t]=(tiers[t]||0)+1;
    const c=d.county||'unknown'; counties[c]=(counties[c]||0)+1;
    const city=(d.city||'Unknown'); cities[city]=(cities[city]||0)+1;
    const ln=(d.lender||'Unknown'); lenders[ln]=(lenders[ln]||0)+1;
    const dv=num(d.mortgage_debt); if(dv&&dv>0) debt+=dv;
    const lg=String(d.lien_gate||'').toLowerCase();
    if (lg.startsWith('pass')) lienPass++; else if (lg) lienFail++;
    if (String(d.val_gate||'').toUpperCase().startsWith('REJECT')) valRej++;
    if (d.hitl_required) hitl++;
  }
  return {tiers,cities,lenders,counties,debt,lienPass,lienFail,valRej,hitl};
}

function barList(container, entries, total, cls){
  const max = Math.max(1, ...entries.map(e=>e[1]));
  $(container).innerHTML = entries.map(([name,n])=>`
    <div class="hbar ${cls||''}">
      <span class="hname" title="${esc(name)}">${esc(name)}</span>
      <span class="htrack"><i style="width:${(n/max*100).toFixed(1)}%"></i></span>
      <span class="hn">${n.toLocaleString()}</span>
    </div>`).join('');
}

function renderOverview(){
  const s = state.summary, deals = state.deals;
  if (!s || !deals.length) return;
  const agg = aggregate(deals);

  $('kpiTotal').textContent = s.total.toLocaleString();
  $('kpiTotalDetail').textContent = `${Object.keys(s.counties).length} counties · ${agg.hitl} HITL-flagged`;
  $('kpiDebt').textContent = fmtMoney(agg.debt, true);
  $('kpiDebtDetail').textContent = `${s.with_debt} deals with recorded debt`;
  const pct = s.total ? (s.geocoded/s.total*100) : 0;
  $('kpiGeo').textContent = pct.toFixed(1)+'%';
  $('kpiGeoDetail').textContent =
    `${s.geocoded.toLocaleString()} points (${esc(s.geocoded_source||'deals')})`;

  const ck = Object.entries(s.counties);
  const cmax = Math.max(1, ...ck.map(e=>e[1]));
  $('kpiCounties').innerHTML = ck.map(([c,n])=>`
    <div class="chip-row">
      <span class="chip-name">${esc(c)}</span>
      <span class="chip-bar"><i style="width:${(n/cmax*100).toFixed(1)}%"></i></span>
      <span class="chip-n">${n.toLocaleString()} · ${(n/s.total*100).toFixed(1)}%</span>
    </div>`).join('');

  barList('tierBars', Object.entries(agg.tiers).sort(), s.total, 'tier');
  barList('cityBars',
    Object.entries(agg.cities).sort((a,b)=>b[1]-a[1]).slice(0,10), s.total);

  $('gateStrip').innerHTML = `
    <span class="gchip pass">LIEN PASS <b>${agg.lienPass}</b></span>
    <span class="gchip fail">LIEN FAIL <b>${agg.lienFail}</b></span>
    <span class="gchip warn">VAL REJECTED <b>${agg.valRej}</b></span>
    <span class="gchip warn">HITL <b>${agg.hitl}</b></span>`;

  const top = Object.entries(agg.lenders).sort((a,b)=>b[1]-a[1]).slice(0,10);
  $('lenderTable').innerHTML =
    '<tr><th>Lender</th><th style="text-align:right">Filings</th></tr>' +
    top.map(([l,n])=>`<tr><td title="${esc(l)}">${esc(l.length>34?l.slice(0,33)+'…':l)}</td>`+
                     `<td class="n">${n}</td></tr>`).join('');

  $('ovSub').textContent = `${s.total} deals · refreshed ${new Date().toLocaleTimeString()}`;
  $('footStamp').textContent = 'data loaded ' + new Date().toLocaleTimeString();
}

/* ================= DEALS TABLE ================= */
const COLUMNS = [
  {key:'address',       label:'Address',   kind:'str'},
  {key:'county',        label:'County',    kind:'str'},
  {key:'city',          label:'City',      kind:'str'},
  {key:'owner',         label:'Owner',     kind:'str'},
  {key:'sqft',          label:'SqFt',      kind:'int'},
  {key:'cad_value',     label:'CAD Value', kind:'money'},
  {key:'mortgage_debt', label:'Debt',      kind:'money'},
  {key:'lender',        label:'Lender',    kind:'str'},
  {key:'ltv_pct',       label:'LTV %',     kind:'pct'},
  {key:'equity_pct',    label:'Equity %',  kind:'pct'},
  {key:'preliminary_mao',label:'MAO',      kind:'money'},
  {key:'lien_gate',     label:'Lien',      kind:'gate'},
  {key:'val_gate',      label:'Val',       kind:'valgate'},
  {key:'blast_tier',    label:'Tier',      kind:'tier'},
];

function renderHead(){
  $('dealsHeadRow').innerHTML = COLUMNS.map(c=>
    `<th class="${['int','money','pct'].includes(c.kind)?'num':''}" data-key="${c.key}">
       ${c.label}<span class="arrow"></span></th>`).join('');
  $('dealsHeadRow').querySelectorAll('th').forEach(th=>{
    th.addEventListener('click', ()=>{
      const key = th.dataset.key;
      state.sort.dir = (state.sort.key===key) ? -state.sort.dir : 1;
      state.sort.key = key; state.page = 0;
      renderTable();
    });
  });
}

function filteredDeals(){
  const f = state.filters;
  const q = f.q.toLowerCase();
  let rows = state.deals.filter(d=>{
    if (f.county && (d.county||'').toLowerCase() !== f.county.toLowerCase()) return false;
    if (f.city && (d.city||'') !== f.city) return false;
    if (f.tier && (d.blast_tier||'') !== f.tier) return false;
    const debt = num(d.mortgage_debt);
    if (f.debtMin != null && !(debt != null && debt >= f.debtMin)) return false;
    if (f.debtMax != null && !(debt != null && debt <= f.debtMax)) return false;
    if (q){
      const hay = [d.address,d.owner,d.lender,d.parcel_id].map(x=>String(x||'')).join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  const col = COLUMNS.find(c=>c.key===state.sort.key) || COLUMNS[0];
  const dir = state.sort.dir;
  rows.sort((a,b)=>{
    let va=a[col.key], vb=b[col.key];
    if (['int','money','pct'].includes(col.kind)){ va=num(va); vb=num(vb); }
    else { va=String(va==null?'':va).toLowerCase(); vb=String(vb==null?'':vb).toLowerCase(); }
    if (va==null || va==='') return  1;   // blanks always sink
    if (vb==null || vb==='') return -1;
    if (va<vb) return -1*dir; if (va>vb) return 1*dir; return 0;
  });
  return rows;
}

function cellHTML(col, d){
  const v = d[col.key];
  switch(col.kind){
    case 'int':  return `<td class="num">${v==null?'—':Number(v).toLocaleString()}</td>`;
    case 'money':return `<td class="num">${fmtMoney(num(v))}</td>`;
    case 'pct':  { const n=num(v); return `<td class="num">${n==null?'—':n.toFixed(1)}</td>`; }
    case 'gate': {
      const g=String(v||'').toLowerCase();
      const cls=g.startsWith('pass')?'pass':(g?'fail':'neutral');
      return `<td><span class="badge ${cls}">${esc(shortWord(v)||'—')}</span></td>`; }
    case 'valgate': {
      const g=String(v||'');
      const up=g.toUpperCase();
      const cls=up.startsWith('PASS')?'pass':(up.startsWith('REJECT')?'rej':'neutral');
      return `<td><span class="badge ${cls}" title="${esc(g)}">${esc(shortWord(g)||'—')}</span></td>`; }
    case 'tier': {
      const t=String(v||'');
      const cls=['t0','t1','t2','t3'].includes(t.toLowerCase())?t.toLowerCase():'neutral';
      return `<td><span class="badge ${cls}">${esc(t||'—')}</span></td>`; }
    default:
      if (col.key==='address') return `<td class="addr" title="${esc(v)}">${esc(v||'—')}</td>`;
      if (col.key==='county')  return `<td class="dim">${esc(String(v||'—'))}</td>`;
      if (col.key==='lender')  return `<td class="dim" title="${esc(v)}" style="max-width:190px;overflow:hidden;text-overflow:ellipsis">${esc(v||'—')}</td>`;
      return `<td>${esc(v==null||v===''?'—':v)}</td>`;
  }
}

function renderTable(){
  // header arrows
  $('dealsHeadRow').querySelectorAll('th').forEach(th=>{
    th.classList.toggle('sorted', th.dataset.key===state.sort.key);
    th.querySelector('.arrow').textContent =
      th.dataset.key===state.sort.key ? (state.sort.dir>0?' ▲':' ▼') : '';
  });

  const rows = filteredDeals();
  const pages = Math.max(1, Math.ceil(rows.length/state.pageSize));
  if (state.page >= pages) state.page = pages-1;
  const start = state.page*state.pageSize;
  const slice = rows.slice(start, start+state.pageSize);

  $('dealsBody').innerHTML = slice.length
    ? slice.map(d=>'<tr class="clickable" data-parcel="'+esc(d.parcel_id||'')+
                   '" title="Click for deal detail">'+COLUMNS.map(c=>cellHTML(c,d)).join('')+'</tr>').join('')
    : '<tr><td colspan="'+COLUMNS.length+'" class="empty-msg">No deals match the current filters.</td></tr>';
  $('dealsBody').querySelectorAll('tr.clickable').forEach(tr=>{
    tr.addEventListener('click', ()=>openDetail(tr.dataset.parcel));
  });

  $('pgLabel').textContent = `page ${state.page+1} / ${pages}`;
  $('matchLabel').textContent =
    `${rows.length.toLocaleString()} of ${state.deals.length.toLocaleString()} deals · showing ${slice.length}`;
  $('dealsSub').textContent = `${rows.length.toLocaleString()} matching deals`;
  $('pgPrev').disabled = state.page===0;
  $('pgNext').disabled = state.page>=pages-1;
}

function populateFilterOptions(){
  const uniq = (fn)=>Array.from(new Set(state.deals.map(fn).filter(Boolean))).sort();
  const fill=(sel, values)=>{
    const cur=$(sel).value;
    $(sel).innerHTML='<option value="">All</option>'+
      values.map(v=>`<option${v===cur?' selected':''}>${esc(v)}</option>`).join('');
  };
  fill('fCounty', uniq(d=>d.county));
  fill('fCity',   uniq(d=>d.city));
  fill('fTier',   uniq(d=>d.blast_tier).sort());
}

function wireFilters(){
  let deb=null;
  $('fSearch').addEventListener('input', e=>{
    clearTimeout(deb);
    deb=setTimeout(()=>{ state.filters.q=e.target.value.trim(); state.page=0; renderTable(); },140);
  });
  $('fCounty').addEventListener('change', e=>{ state.filters.county=e.target.value; state.page=0; renderTable(); });
  $('fCity')  .addEventListener('change', e=>{ state.filters.city=e.target.value;   state.page=0; renderTable(); });
  $('fTier')  .addEventListener('change', e=>{ state.filters.tier=e.target.value;   state.page=0; renderTable(); });
  $('fDebtMin').addEventListener('change', e=>{ state.filters.debtMin=num(e.target.value); state.page=0; renderTable(); });
  $('fDebtMax').addEventListener('change', e=>{ state.filters.debtMax=num(e.target.value); state.page=0; renderTable(); });
  $('clearFilters').addEventListener('click', ()=>{
    state.filters={q:'',county:'',city:'',tier:'',debtMin:null,debtMax:null};
    $('fSearch').value=''; $('fCounty').value=''; $('fCity').value='';
    $('fTier').value=''; $('fDebtMin').value=''; $('fDebtMax').value='';
    state.page=0; renderTable();
  });
  $('pgPrev').addEventListener('click', ()=>{ if(state.page>0){state.page--; renderTable();} });
  $('pgNext').addEventListener('click', ()=>{ state.page++; renderTable(); });
}

/* ================= MAP ================= */
const CATEGORY_COLORS = {
  'standard':'#95a5a6','high-equity':'#2ecc71','high equity':'#2ecc71',
  'distressed':'#e67e22','foreclosure':'#e05c5c',
};
const TIER_RADIUS = { T0:5, T1:6, T2:8, T3:11 };

function featureOf(f){
  const p = (f.properties||{});
  const coords = (f.geometry && f.geometry.coordinates) || [];
  return {
    lng: parseCoord(coords[0]), lat: parseCoord(coords[1]),
    address:p.address||p.title||'—', city:p.city||'', county:p.county||'',
    category:String(p.category||'Standard'), color:p['marker-color']||'#95a5a6',
    appraised:num(p.appraised_value), ppsf:num(p.price_per_sqft),
    dealType:p.deal_type||'', tier:String(p.blast_tier||''),
  };
}

function initMap(){
  if (typeof L === 'undefined'){           // CDN unreachable (offline) — degrade
    $('map').innerHTML = '<div class="map-offline">Leaflet failed to load (offline?). '+
      'The map view requires network access to unpkg.com — all other views work offline.</div>';
    return;
  }
  if (state.map.inited){ state.map.map.invalidateSize(); rebuildMarkers(); return; }
  state.map.inited = true;

  const map = L.map('map',{ preferCanvas:true }).setView([33.05,-96.8],9);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{
    attribution:'© OpenStreetMap contributors', maxZoom:19,
  }).addTo(map);
  state.map.layer = L.layerGroup().addTo(map);
  state.map.map = map;

  fetchJSON('/api/geojson')
    .then(gj=>{
      state.map.features = ((gj&&gj.features)||[]).map(featureOf)
        .filter(f=>f.lat!=null && f.lng!=null && f.lat!==0 && f.lng!==0);
      const uniq=(fn)=>Array.from(new Set(state.map.features.map(fn).filter(Boolean))).sort();
      const fill=(sel,values)=>{
        const cur=$(sel).value;
        $(sel).innerHTML='<option value="">All</option>'+
          values.map(v=>`<option${v===cur?' selected':''}>${esc(v)}</option>`).join('');
      };
      fill('mCounty', uniq(f=>f.county)); fill('mTier', uniq(f=>f.tier));
      rebuildMarkers();
    })
    .catch(err=>showError('mapError','Map data unavailable: '+err.message));

  $('mCounty').addEventListener('change', e=>{ state.map.county=e.target.value; rebuildMarkers(); });
  $('mTier')  .addEventListener('change', e=>{ state.map.tier=e.target.value;  rebuildMarkers(); });
}

function popupHTML(f){
  return `<div style="min-width:220px;font:12.5px/1.4 sans-serif">
    <div style="font-weight:700;margin-bottom:3px">${esc(f.address)}</div>
    <div style="color:#666">${esc(f.city)} · ${esc(f.county)}</div>
    <div style="margin:5px 0">
      <span class="badge neutral">${esc(f.category)}</span>
      ${f.tier?`<span class="badge ${esc(f.tier.toLowerCase())}">${esc(f.tier)}</span>`:''}
      ${f.dealType?`<span class="badge neutral">${esc(f.dealType)}</span>`:''}
    </div>
    <div>CAD appraised: <b>${fmtMoney(f.appraised)}</b></div>
    <div>Price/sqft: <b>${f.ppsf!=null?f.ppsf.toFixed(2):'—'}</b></div>
  </div>`;
}

function rebuildMarkers(){
  const layer = state.map.layer;
  if (!layer) return;
  layer.clearLayers();
  const cf=state.map.county.toLowerCase(), tf=state.map.tier;
  let shown=0;
  for (const f of state.map.features){
    if (cf && (f.county||'').toLowerCase()!==cf) continue;
    if (tf && f.tier!==tf) continue;
    shown++;
    const color = CATEGORY_COLORS[f.category.toLowerCase()] || f.color || '#95a5a6';
    L.circleMarker([f.lat,f.lng],{
      radius:TIER_RADIUS[f.tier]||5, color:color, weight:1.5,
      fillColor:color, fillOpacity:.65,
    }).bindPopup(popupHTML(f)).addTo(layer);
  }
  const sub=document.querySelector('#view-map .sub');
  if (sub) sub.textContent =
    `live_foreclosures.geojson · ${shown} / ${state.map.features.length} features plotted`;
}

/* ================= DEAL DETAIL (F3/F4) ================= */
/* Provenance-wrapped fields arrive as {value, source, retrieved_at, confidence}. */
function pvWrap(v){
  return v && typeof v==='object' && !Array.isArray(v) && ('value' in v) ? v : null;
}
function pvVal(v){ const p=pvWrap(v); return p ? p.value : v; }

function kvRow(label, raw, fmt){
  const p = pvWrap(raw);
  let val = p ? p.value : raw;
  if (val==null || val==='') return '';
  if (fmt) val = fmt(val);
  const conf = p ? '<td class="n">'+Math.round((p.confidence||0)*100)+'%</td>' : '<td class="n">—</td>';
  return '<tr><td>'+esc(label)+'</td><td>'+esc(String(val))+'</td>'+conf+'</tr>';
}

function kvTable(elId, pairs){
  const html = pairs.map(pr=>kvRow(pr[0], pr[1], pr[2])).join('');
  $(elId).innerHTML = html || '<tr><td colspan="3" class="empty-msg">no data recorded</td></tr>';
}

function gateChip(label, value, cls){
  return '<span class="gchip '+(cls||'')+'">'+esc(label)+' <b>'+esc(value||'—')+'</b></span>';
}

function renderDetail(cpr){
  $('detailPlaceholder').style.display='none';
  $('detailBody').style.display='block';
  $('detailSub').textContent =
    (cpr.situs_address||'(no address)')+' · '+esc(cpr.county||'?')+' · '+(cpr.channel||'no channel');

  kvTable('snapshotTable', [
    ['Address',   cpr.situs_address],
    ['Legal',     cpr.legal_description],
    ['Type',      cpr.property_type],
    ['SqFt',      cpr.improvement_sqft],
    ['Land SqFt', cpr.land_sqft],
    ['Beds',      cpr.beds],
    ['Baths',     cpr.baths],
    ['Stories',   cpr.stories],
    ['Year built',cpr.year_built],
    ['Pool',      pvVal(cpr.pool)==null?null:cpr.pool],
    ['Homestead', pvVal(cpr.homestead)==null?null:cpr.homestead],
    ['ISD',       cpr.isd],
  ]);

  kvTable('ownerTable', [
    ['Owner',        cpr.owner_name],
    ['Mailing addr', cpr.owner_mailing_address],
    ['Own yrs',      cpr.ownership_length_years],
    ['Absentee',     cpr.absentee],
    ['Estate',       cpr.deceased_estate],
    ['Tax delinquent', cpr.tax_delinquent],
    ['Delinq amount',  cpr.tax_delinquent_amount, fmtMoney],
    ['Tax suit case',  cpr.tax_suit_case_number],
    ['Channel',      cpr.channel],
    ['Deal type',    cpr.deal_type],
    ['NOD status',   cpr.nod_status],
    ['Sale date',    cpr.sale_date],
    ['Opening bid',  cpr.opening_bid, fmtMoney],
    ['Redemption',   cpr.redemption_class],
    ['Redemption exp.', cpr.redemption_expiry],
  ]);

  /* Valuation & gates */
  const cadP = pvWrap(cpr.cad_appraised_value);
  const val = cpr.valuation || {};
  const arvP = pvWrap(val.arv);
  const compSet = Array.isArray(val.comp_set) ? val.comp_set : [];
  $('valuationBox').innerHTML =
    '<div class="gate-chips">'+
      gateChip('COLUMN', cpr.column, 'pass')+
      gateChip('TIER', String(cpr.blast_tier==null?'—':cpr.blast_tier), 'warn')+
      gateChip('LIEN LOOKUP', cpr.lien_lookup_status,
               cpr.lien_lookup_status==='ok'?'pass':(cpr.lien_lookup_status==='error'?'fail':''))+
      gateChip('GATE-VAL', val.gate_val_verdict||'none',
               val.gate_val_verdict==='pass'?'pass':(val.gate_val_verdict?'fail':''))+
      (cpr.dead_reason?gateChip('DEAD', cpr.dead_reason, 'fail'):'')+
    '</div>'+
    '<table class="mini kv">'+
      '<tr><td>CAD appraised</td><td>'+(cadP?fmtMoney(num(cadP.value)):'—')+'</td>'+
        '<td class="n">'+(cadP?Math.round((cadP.confidence||0)*100)+'%':'—')+'</td></tr>'+
      '<tr><td>ARV (HITL)</td><td>'+(arvP?fmtMoney(num(arvP.value)):'—')+'</td>'+
        '<td class="n">'+(arvP?Math.round((arvP.confidence||0)*100)+'%':'—')+'</td></tr>'+
      '<tr><td>Comp source</td><td colspan="2">'+esc(String(val.comp_source||'—'))+
        ' · '+compSet.length+' comps</td></tr>'+
      (val.gate_val_reason?'<tr><td>GATE-VAL reason</td><td colspan="2" class="dim">'+esc(String(val.gate_val_reason))+'</td></tr>':'')+
    '</table>';

  /* Liens */
  const liens = Array.isArray(cpr.liens) ? cpr.liens : [];
  $('lienTable').innerHTML =
    '<tr><th>Type</th><th class="n">Amount</th><th>Recorded</th><th>Lender</th>'+
    '<th class="n">Pri</th><th title="Survives foreclosure">FC</th><th title="Survives tax sale">TS</th></tr>'+
    (liens.length ? liens.map(l=>
      '<tr><td>'+esc(l.lien_type||'—')+'</td>'+
      '<td class="n">'+(num(l.amount)!=null?fmtMoney(num(l.amount)):((l.amount!=null)?String(l.amount):'—'))+'</td>'+
      '<td class="dim">'+esc(l.recording_date||'—')+'</td>'+
      '<td class="dim" title="'+esc(l.lender||'')+'">'+esc(l.lender||'—')+'</td>'+
      '<td class="n">'+(l.priority!=null?l.priority:'—')+'</td>'+
      '<td>'+(l.survives_foreclosure?'<span class="badge fail">yes</span>':'<span class="badge neutral">no</span>')+'</td>'+
      '<td>'+(l.survives_tax_sale?'<span class="badge fail">yes</span>':'<span class="badge neutral">no</span>')+'</td>'+
    '</tr>').join('')
    : '<tr><td colspan="7" class="empty-msg">No liens recorded.</td></tr>');

  /* Provenance ledger */
  const ledger = Array.isArray(cpr.ledger) ? cpr.ledger : [];
  const MAX_LEDGER_ROWS = 250;
  $('ledgerMeta').textContent = ledger.length ? ('· '+ledger.length+' receipts'+
    (ledger.length>MAX_LEDGER_ROWS?' (showing first '+MAX_LEDGER_ROWS+')':'')) : '';
  $('ledgerTable').innerHTML =
    '<tr><th>Field</th><th>Old → New</th><th>Source</th><th>Timestamp</th><th class="n">Note/Hash</th></tr>'+
    (ledger.length ? ledger.slice(0,MAX_LEDGER_ROWS).map(e=>{
      const oldV = e.old_value==null?'∅':String(e.old_value).slice(0,60);
      const newV = e.new_value==null?'∅':String(e.new_value).slice(0,60);
      return '<tr><td>'+esc(e.field_name||'—')+'</td>'+
        '<td title="'+esc(oldV+' → '+newV)+'"><span class="dim">'+esc(oldV)+'</span> → '+esc(newV)+'</td>'+
        '<td class="dim">'+esc(e.source||'—')+'</td>'+
        '<td class="dim">'+esc(e.timestamp||'—')+'</td>'+
        '<td class="n dim" title="'+esc(e.entry_hash||'')+'">'+esc(e.note||e.entry_hash||'—')+'</td></tr>';
    }).join('')
    : '<tr><td colspan="5" class="empty-msg">Ledger empty.</td></tr>');
}

async function fetchMemo(parcel){
  $('memoBody').innerHTML='<p class="dim">loading…</p>';
  try{
    const payload = await fetchJSON('/api/memo/'+encodeURIComponent(parcel));
    $('memoBody').innerHTML = mdToHtml(payload.content||'(empty memo)');
  }catch(err){
    $('memoBody').innerHTML = /HTTP 404/.test(err.message)
      ? '<p class="dim">No deal memo on disk for this parcel yet.</p>'
      : '<p class="dim">Memo unavailable: '+esc(err.message)+'</p>';
  }
}

async function openDetail(parcel){
  if (!parcel) return;
  state.detail = { parcel: parcel, memoLoaded: true };
  switchView('detail');
  showError('detailError','');
  $('offerResultCard').style.display='none';
  $('clerkResultCard').style.display='none';
  $('detailSub').textContent = 'parcel '+parcel+' — loading CPR snapshot…';

  // Memo is independent of the CPR snapshot — fetch both, degrade separately.
  fetchMemo(parcel);

  try{
    const cpr = await fetchJSON('/api/cpr/'+encodeURIComponent(parcel));
  /* Audit HITL-5: comp-store read surface — show the stored comp count on
     the detail view (read-only; seeding preview via /api/comps/). */
  fetchJSON('/api/comps/'+encodeURIComponent(parcel))
    .then(function (cj) {
      var chip = document.getElementById('compsCount');
      if (chip) {
        chip.hidden = false;
        chip.textContent = 'comps in store: ' + (cj.comp_count || 0) +
          (cj.seed_available ? ' · seed available' : '');
      }
    })
    .catch(function () { /* comps surface is additive — ignore */ });
    renderDetail(cpr);
    const owner = pvVal(cpr.owner_name);
    if (!$('clerkParty').value && owner) $('clerkParty').value = String(owner);
  }catch(err){
    $('detailPlaceholder').style.display='block';
    $('detailPlaceholder').innerHTML =
      '<div style="color:var(--amber)">CPR snapshot unavailable for parcel <b>'+esc(parcel)+
      '</b></div><p class="dim" style="margin-top:8px">'+esc(err.message)+'</p>';
    $('detailSub').textContent = 'parcel '+parcel+' — no CPR on disk';
  }
}

async function runGenerateOffer(){
  const parcel = state.detail.parcel; if (!parcel) return;
  const btn = $('offerBtn');
  btn.disabled = true; btn.textContent = '⏳ Pricing…';
  showError('detailError','');
  try{
    const body = await fetch('/api/generate-offer', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({parcel_id: parcel}),
    }).then(r=>r.json().then(j=>({ok:r.ok, status:r.status, json:j})));
    if (!body.ok) throw new Error('generate-offer → HTTP '+body.status+': '+(body.json.detail||''));
    const j = body.json;
    $('offerResultCard').style.display='block';
    $('offerDeadNote').style.display = j.dead ? 'block':'none';
    $('offerDeadNote').textContent = j.note || '';
    const t = j.terms || {};
    $('offerTerms').innerHTML = j.dead ? '' : [
      ['Offer', t.offer_price], ['Earnest', t.earnest_money], ['Option fee', t.option_fee],
      ['Option', t.option_period_days!=null?t.option_period_days+'d':null],
      ['Close', t.closing_timeline_days!=null?t.closing_timeline_days+'d':null],
      ['Financing', t.is_cash?'Cash':'Financed'],
    ].map(ch=>'<span class="term-chip">'+ch[0]+'<b>'+
             (typeof ch[1]==='number'?fmtMoney(ch[1]):esc(String(ch[1]==null?'—':ch[1])))+'</b></span>').join('') +
      (t.blocked ? '<span class="term-chip" style="border-color:#6d3030;color:#f0b9b9">BLOCKED<b>'+esc(t.block_reason||'')+'</b></span>' : '');
    $('offerLetter').style.display = j.dead ? 'none':'block';
    $('offerLetter').textContent = j.letter||'';
  }catch(err){
    showError('detailError','Could not generate offer draft: '+err.message);
  }finally{
    btn.disabled=false; btn.textContent='✎ Generate draft offer';
  }
}

async function runClerkSearch(){
  const party = $('clerkParty').value.trim();
  if (!party){ showError('detailError','Enter a party name for the clerk search.'); return; }
  if (!window.confirm('Run LIVE Collin County clerk search for "'+party+'"?\n'+
                      'One polite request against public records (~10–15 s).')) return;
  const btn = $('clerkBtn');
  btn.disabled = true; btn.textContent = '⏳ Searching county records…';
  showError('detailError','');
  try{
    const resp = await fetch('/api/clerk-search', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({party_name: party}),
    });
    const j = await resp.json();
    if (!resp.ok) throw new Error('clerk-search → HTTP '+resp.status+': '+(j.detail||''));
    $('clerkResultCard').style.display='block';
    $('clerkSummary').innerHTML =
      '<b>'+esc(j.party_name)+'</b>: <span style="color:var(--accent)">'+j.instrument_count+
      '</span> instruments · <span style="color:var(--amber)">'+j.encumbrance_count+
      '</span> encumbrances'+(j.preview_truncated?' · preview shows first '+j.preview.length:'');
    $('clerkPreview').innerHTML =
      '<tr><th>Doc #</th><th>Type</th><th>Recorded</th><th>Parties</th><th>Legal</th></tr>'+
      ((j.preview&&j.preview.length) ? j.preview.map(d=>
        '<tr><td class="n">'+esc(d.doc_number||'—')+'</td>'+
        '<td>'+esc(d.doc_type||'—')+'</td>'+
        '<td class="dim">'+esc(d.recorded_date||'—')+'</td>'+
        '<td class="dim">'+esc((d.parties||[]).join(' / ')||'—')+'</td>'+
        '<td class="dim" title="'+esc(d.legal_description||'')+'">'+esc((d.legal_description||'—').slice(0,48))+'</td></tr>'
      ).join('') : '<tr><td colspan="5" class="empty-msg">No instruments returned.</td></tr>');
    $('clerkPoliteness').textContent = j.politeness||'';
  }catch(err){
    showError('detailError','Clerk search failed: '+err.message);
  }finally{
    btn.disabled=false; btn.textContent='⚖ Search Collin clerk';
  }
}

/* ================= MARKET (F11) ================= */
async function loadMarket(){
  const g = $('mktGroupby').value || state.market.groupby;
  state.market.groupby = g;
  showError('mktError','');
  $('marketBody').innerHTML = '<tr><td colspan="6" class="empty-msg">computing…</td></tr>';
  try{
    const payload = await fetchJSON('/api/market?groupby='+encodeURIComponent(g));
    const rows = payload.groups||[];
    $('marketBody').innerHTML = rows.length ? rows.map(r=>
      '<tr><td class="addr" title="'+esc(r.group)+'">'+esc(r.group)+'</td>'+
      '<td class="num">'+r.deals.toLocaleString()+'</td>'+
      '<td class="num">'+(r.median_cad!=null?fmtMoney(r.median_cad):'—')+'</td>'+
      '<td class="num">'+(r.total_debt!=null?fmtMoney(r.total_debt,true):'—')+'</td>'+
      '<td class="num">'+(r.positive_equity_share!=null?(r.positive_equity_share*100).toFixed(1)+'%':'—')+'</td>'+
      '<td class="num">'+(r.upcoming_sales||0)+'</td></tr>').join('')
      : '<tr><td colspan="6" class="empty-msg">No deals to aggregate.</td></tr>';
    document.querySelector('#view-market .sub').textContent =
      rows.length+' groups over '+rows.reduce((a,r)=>a+r.deals,0)+' deals · groupby='+payload.groupby;
    state.market.loaded = true;
  }catch(err){
    $('marketBody').innerHTML = '<tr><td colspan="6" class="empty-msg">unavailable</td></tr>';
    showError('mktError','Market aggregates unavailable: '+err.message);
  }
}

/* ================= STATS ================= */
function mdToHtml(md){
  // Minimal markdown→text renderer: headings, bullets, bold, pipe tables.
  const lines = esc(md).split(/\r?\n/);
  let html='', list=false, table=false;
  const closeList=()=>{ if(list){html+='</ul>'; list=false;} };
  const closeTable=()=>{ if(table){html+='</tbody></table>'; table=false;} };
  const inline=(s)=>s.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
  for (const raw of lines){
    const line=raw.trimEnd();
    if (/^\|/.test(line)){
      closeList();
      const cells=line.split('|').slice(1,-1).map(c=>c.trim());
      if (cells.every(c=>/^:?-{2,}:?$/.test(c))) continue;   // separator row
      if (!table){ html+='<table><tbody>'; table=true; }
      html+='<tr>'+cells.map(c=>'<td>'+inline(c)+'</td>').join('')+'</tr>';
      continue;
    }
    closeTable();
    if (/^#\s/.test(line)){ closeList(); html+='<h1>'+inline(line.slice(2))+'</h1>'; }
    else if (/^##\s/.test(line)){ closeList(); html+='<h2>'+inline(line.slice(3))+'</h2>'; }
    else if (/^-\s/.test(line)){
      if(!list){ html+='<ul>'; list=true; }
      html+='<li>'+inline(line.slice(2))+'</li>';
    }
    else if (line===''){ closeList(); }
    else { closeList(); html+='<p>'+inline(line)+'</p>'; }
  }
  closeList(); closeTable();
  return html;
}

async function loadStats(){
  showError('statsError','');
  $('statsBody').innerHTML='<p class="dim">Computing portfolio statistics…</p>';
  try{
    const payload = await fetchJSON('/api/stats');
    $('statsBody').innerHTML = mdToHtml(payload.report||'(empty report)') +
      `<div class="meta-line">enriched ${payload.enriched} records · skipped ${payload.skipped}`+
      ` · computed ${new Date().toLocaleTimeString()}</div>`;
  }catch(err){
    $('statsBody').innerHTML='<p class="dim">unavailable</p>';
    showError('statsError','Stats unavailable: '+err.message);
  }
}

/* --- weekly digest (audit R-6/ORCH-4: artifact-only surface now served) --- */
async function loadDigest(){
  try{
    const d = await fetchJSON('/api/digest');
    $('digestFile').textContent = d.file || '';
    $('digestBody').textContent = d.content || '(empty digest)';
  }catch(err){
    $('digestFile').textContent = '';
    $('digestBody').textContent = 'digest unavailable: ' + err.message;
  }
}

/* ================= BOOT ================= */
function setStatus(ok, text){
  $('statusDot').className = 'dot'+(ok?' ok':'');
  $('statusText').textContent = text;
}

async function boot(){
  renderHead(); wireFilters();
  try{
    const [summary, deals] = await Promise.all([
      fetchJSON('/api/summary'), fetchJSON('/api/deals'),
    ]);
    state.summary=summary; state.deals=deals;
    setStatus(true,'API live');
    renderOverview();
    populateFilterOptions();
    renderTable();
    loadDigest();
  }catch(err){
    setStatus(false,'API offline');
    showError('ovError','Data unavailable: '+err.message);
    showError('dealsError','Data unavailable: '+err.message);
  }
}

$('refreshAllBtn').addEventListener('click', boot);
$('statsRefresh').addEventListener('click', loadStats);

/* --- detail + market wiring (F3/F4/F11) --- */
$('detailBack').addEventListener('click', ()=>switchView('deals'));
$('offerBtn').addEventListener('click', runGenerateOffer);
$('clerkBtn').addEventListener('click', runClerkSearch);
$('mktRefresh').addEventListener('click', loadMarket);
$('mktGroupby').addEventListener('change', loadMarket);

boot();
