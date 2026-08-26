'use strict';
/* =========================================================================
 * formula-engine.js — HERMES-REO screen V · Underwriting (the flagship)
 *
 * Ports the workbook's live formulas (build_workbook.py) and the pure
 * models in src/underwriting.py to plain browser ES6: flip P&L, BRRRR
 * recycle, rental DCF, tax-deed redemption yield and auction bid ceiling,
 * plus the GATE-VAL verdict that governs whether the ARV may be trusted.
 * No frameworks, no build step, no randomness — every figure derives from
 * the shared assumption set.
 *
 * PARITY CONTRACT — test_recalc.py (repo root): the workbook acceptance
 * suite pins recalculated headline outputs with a $1 balance tolerance
 * (BALANCE_TOLERANCE = 1.0), asserts GATE-VAL is live and PASSING, and
 * checks the tax-deed redemption premium at exactly 25% of the bid. The
 * self-test block below mirrors those assertions against the same fixture
 * deal (ARV $750k / purchase $475k / rehab $50k), so the desk UI and the
 * Excel model cannot drift apart silently.
 *
 * Performance: any input change recomputes ALL models and lands every DOM
 * write inside ONE requestAnimationFrame callback (<16 ms frame budget).
 * ========================================================================= */
(function () {

  // ---- model constants (mirror src/policy.py + workbook named ranges) ----
  const N_MONTHS = 360;              // 30-yr amortization
  const OPEX_RATIO = 0.60;           // NOI = rent × 12 × 60% (40% opex load)
  const REFI_LTV = 0.75;             // BRRRR cash-out refinance LTV
  const LOAN_LTV = 0.75;             // rental acquisition loan LTV
  const DOWN_PCT = 0.25;             // rental down payment
  const REDEMPTION_PREMIUM = 0.25;   // TX tax-sale statutory premium
  const REDEMPTION_DAYS = 180;       // homestead redemption window
  const LIEN_HAIRCUT = 18000;        // surviving-lien haircut (pri-8 tax lien)
  const OPEN_PCT = 0.85;             // offer ladder opening (% of MAO)
  const WALK_PCT = 1.05;             // offer ladder walk-away (% of MAO)
  const DSCR_FLOOR = 1.25;
  const SENS_GOOD_AT = 60000;        // sensitivity color-band threshold

  const COMP_FLOORS = {
    mls_agent: 0.85,
    appraiser_bpo: 0.75,
    title_feed: 0.60,
    vendor_avm: 0.50,
    assessed_only: null,             // rejected source — never passes
    zestimate_only: null,
  };
  const REJECTED_SOURCES = { assessed_only: true, zestimate_only: true };
  const SENS_ARV_DELTAS = [10, 5, 0, -5, -10];
  const SENS_REHAB_DELTAS = [-10, 0, 10, 20];

  // ------------------------------------------------------------ formatters
  function money(n) {
    if (!isFinite(n)) return '—';
    return (n < 0 ? '−$' : '$') + Math.abs(Math.round(n)).toLocaleString('en-US');
  }
  function pct(x, d) {
    if (!isFinite(x)) return '—';
    return (x * 100).toFixed(d === undefined ? 1 : d) + '%';
  }
  /** Percent for inline labels ("+20%", "6.10%") — trims trailing zeros. */
  function pctLabel(fraction) {
    if (!isFinite(fraction)) return '—';
    const v = Math.round(fraction * 1000) / 10;
    return (v % 1 === 0 ? v.toFixed(0) : v.toFixed(v * 10 % 1 === 0 ? 1 : 2)) + '%';
  }

  // ----------------------------------------------------------- pure engine
  /**
   * Monthly payment on a fully amortizing loan.
   * pmt(L, rAnnual, n) = L·(r/12) / (1 − (1+r/12)^−n), n defaults to 360.
   * Returns 0 for non-positive principals or non-finite rates so DSCR-style
   * ratios degrade to 0 instead of NaN (deterministic output).
   */
  function pmt(L, rAnnual, n) {
    const term = n || N_MONTHS;
    const r = rAnnual / 12;
    if (!isFinite(L) || L <= 0 || !isFinite(r)) return 0;
    if (r === 0) return L / term;
    return (L * r) / (1 - Math.pow(1 + r, -term));
  }

  /**
   * Recompute every model from one assumption set. `a` holds base units:
   * arv, purchase, rehab, rent, holding, financing, closing (dollars);
   * contingency, maoFactor, loanRate, confidence (fractions); compSource key.
   */
  function computeAll(a) {
    const rehabTotal = a.rehab * (1 + a.contingency);
    const totalCost = a.purchase + rehabTotal + a.holding + a.financing + a.closing;

    // Flip (src/underwriting.py::flip_pnl)
    const netProfit = a.arv - totalCost;
    const roi = totalCost > 0 ? netProfit / totalCost : 0;
    const margin = a.arv > 0 ? netProfit / a.arv : 0;
    const mao = a.arv * a.maoFactor - a.rehab;

    const noi = a.rent * 12 * OPEX_RATIO;
    const debtPerYear = (L) => pmt(L, a.loanRate) * 12;

    // BRRRR recycle
    const refiLoan = a.arv * REFI_LTV;
    const cashLeft = totalCost - refiLoan;
    const recycled = totalCost > 0 ? refiLoan / totalCost : 0;
    const dsBrrrr = debtPerYear(refiLoan);
    const dscrBrrrr = dsBrrrr > 0 ? noi / dsBrrrr : 0;

    // Rental DCF at 25% down
    const loanRental = a.purchase * LOAN_LTV;
    const downRental = a.purchase * DOWN_PCT;
    const dsRental = debtPerYear(loanRental);
    const capRate = a.purchase > 0 ? noi / a.purchase : 0;
    const dscrRental = dsRental > 0 ? noi / dsRental : 0;
    const cashFlow = noi - dsRental;
    const coc = downRental > 0 ? cashFlow / downRental : 0;

    // Tax-deed redemption yield (test_recalc.py [6]: premium == bid × 25%)
    const bid = a.purchase;
    const redemptionReturn = bid * REDEMPTION_PREMIUM;
    const annualized = Math.pow(1 + REDEMPTION_PREMIUM, 365 / REDEMPTION_DAYS) - 1;
    const equityIfAcquired = a.arv - bid - rehabTotal;

    // Auction bid ceiling
    const bidCeiling = mao - LIEN_HAIRCUT;
    const maxCash = bidCeiling + rehabTotal;

    // GATE-VAL verdict (exact spec order)
    const floor = COMP_FLOORS[a.compSource];
    let verdict, reason;
    if (REJECTED_SOURCES[a.compSource]) {
      verdict = 'FAIL';
      reason = 'Rejected source — assessed value / Zestimate cannot set ARV in a non-disclosure state.';
    } else if (typeof floor === 'number' && a.confidence < floor) {
      verdict = 'FAIL';
      reason = 'Confidence ' + a.confidence.toFixed(2) + ' below floor ' + floor.toFixed(2) + '.';
    } else if (a.compSource === 'vendor_avm') {
      verdict = 'WARN';
      reason = 'Vendor AVM — directional only; treat the ARV as soft and widen your margin.';
    } else {
      verdict = 'PASS';
      reason = 'Confidence ' + a.confidence.toFixed(2) + ' ≥ floor ' +
               (typeof floor === 'number' ? floor.toFixed(2) : '—') + ' — ARV trusted.';
    }
    const untrusted = verdict === 'FAIL';

    return {
      rehabTotal, totalCost, netProfit, roi, margin, mao, noi,
      brrrr: { refiLoan, cashLeft, recycled, dscr: dscrBrrrr },
      rental: { capRate, dscr: dscrRental, cashFlow, coc, down: downRental },
      taxdeed: { bid, redemptionReturn, annualized, equityIfAcquired },
      auction: { bidCeiling, maxCash },
      gate: { verdict, reason, untrusted, floor: typeof floor === 'number' ? floor : null },
    };
  }

  // ------------------------------------------------------- metric builders
  function metricDefs(model, c, s) {
    const mk = (label, value, tone) => ({ label, value, tone });
    switch (model) {
      case 'brrrr':
        return [
          mk('Refi loan · 75% ARV', money(c.brrrr.refiLoan), 'plain'),
          mk('Cash left in', money(c.brrrr.cashLeft), c.brrrr.cashLeft <= 0 ? 'good' : 'bad'),
          mk('% recycled', pct(c.brrrr.recycled, 0), 'plain'),
          mk('DSCR at refi', c.brrrr.dscr.toFixed(2), c.brrrr.dscr >= DSCR_FLOOR ? 'good' : 'bad'),
        ];
      case 'rental':
        return [
          mk('Cap rate', pct(c.rental.capRate, 2), 'plain'),
          mk('DSCR', c.rental.dscr.toFixed(2), c.rental.dscr >= DSCR_FLOOR ? 'good' : 'bad'),
          mk('Annual cash flow', money(c.rental.cashFlow), c.rental.cashFlow >= 0 ? 'good' : 'bad'),
          mk('Cash-on-cash', pct(c.rental.coc), c.rental.coc >= 0 ? 'good' : 'bad'),
        ];
      case 'taxdeed':
        return [
          mk('Winning bid', money(c.taxdeed.bid), 'plain'),
          mk('Redemption return · 25%', money(c.taxdeed.redemptionReturn), 'good'),
          mk('Annualized yield', pct(c.taxdeed.annualized), 'good'),
          mk('Equity if acquired', money(c.taxdeed.equityIfAcquired),
             c.taxdeed.equityIfAcquired >= 0 ? 'good' : 'bad'),
        ];
      case 'auction':
        return [
          mk('MAO · ' + pctLabel(s.maoFactor), money(c.mao), 'plain'),
          mk('Surviving-lien haircut', money(LIEN_HAIRCUT), 'bad'),
          mk('Bid ceiling', money(c.auction.bidCeiling), 'accent'),
          mk('Max cash needed', money(c.auction.maxCash), 'plain'),
        ];
      default: // flip
        return [
          mk('Net profit', money(c.netProfit), c.netProfit >= 0 ? 'good' : 'bad'),
          mk('ROI', pct(c.roi), 'plain'),
          mk('Margin', pct(c.margin), 'plain'),
          mk('MAO · ' + pctLabel(s.maoFactor), money(c.mao), 'accent'),
        ];
    }
  }

  const MODEL_NOTES = {
    flip: 'Buy → rehab → resell. Net profit is ARV less purchase, rehab (+contingency), holding, financing and closing. MAO is the 70%-rule ceiling.',
    brrrr: 'Buy, rehab, rent, refinance at 75% of ARV, repeat. Lenders want DSCR ≥ 1.25 to refinance; below that the rent will not carry the new loan.',
    rental: 'Buy-and-hold at 25% down. NOI assumes a 40% operating-expense load on gross rent; cap rate is NOI over purchase, cash-on-cash is levered cash flow over the down payment.',
    taxdeed: 'Two branches: if the owner redeems, Texas pays a 25% premium (a high annualized yield over the 180-day homestead period); if not, you acquire at bid and the equity is ARV less bid and rehab.',
    auction: 'Courthouse-steps bid ceiling: the MAO less a haircut for liens that survive the foreclosure (here the priority-8 tax lien). Sight-unseen, cash, no title insurance — bid the ceiling, never above.',
  };

  /** Sensitivity rows: ARV Δ [+10,+5,0,−5,−10] × rehab Δ [−10,0,+10,+20]. */
  function sensitivityGrid(a, rehabTotal) {
    const band = (p) => p <= 0 ? 'band-neg' : (p < SENS_GOOD_AT ? 'band-mid' : 'band-pos');
    return SENS_ARV_DELTAS.map((ad) => ({
      label: (ad > 0 ? '+' : '') + ad + '%',
      cells: SENS_REHAB_DELTAS.map((rd) => {
        const arv = a.arv * (1 + ad / 100);
        const rt = rehabTotal * (1 + rd / 100);
        const p = arv - a.purchase - rt - a.holding - a.financing - a.closing;
        return {
          text: (p < 0 ? '−$' : '$') + Math.abs(Math.round(p / 1000)) + 'k',
          cls: band(p),
        };
      }),
    }));
  }

  /** Eight-model roll-up (five live + three static postures). */
  function rollupRows(a, c) {
    const ok = '#3f5d46', bad = '#8a2f22', neutral = 'var(--color-neutral-600)';
    return [
      { model: 'Fix & Flip', out: money(c.netProfit) + ' net · ' + pct(c.roi) + ' ROI',
        note: 'GATE-VAL ' + c.gate.verdict, noteColor: c.gate.untrusted ? bad : ok },
      { model: 'BRRRR', out: money(c.brrrr.refiLoan) + ' refi · ' + pct(c.brrrr.recycled, 0) + ' recycled',
        note: c.brrrr.dscr >= DSCR_FLOOR ? 'DSCR ' + c.brrrr.dscr.toFixed(2) + ' — refis'
                                         : 'DSCR ' + c.brrrr.dscr.toFixed(2) + ' — below 1.25',
        noteColor: c.brrrr.dscr >= DSCR_FLOOR ? ok : bad },
      { model: 'Rental DCF', out: pct(c.rental.capRate, 2) + ' cap · DSCR ' + c.rental.dscr.toFixed(2),
        note: c.rental.cashFlow >= 0 ? money(c.rental.cashFlow) + '/yr cash flow'
                                     : money(c.rental.cashFlow) + '/yr — negative',
        noteColor: c.rental.cashFlow >= 0 ? ok : bad },
      { model: 'Tax-Deed', out: money(c.taxdeed.redemptionReturn) + ' · ' + pct(c.taxdeed.annualized) + ' annualized',
        note: 'redemption class required', noteColor: neutral },
      { model: 'Auction Bid', out: money(c.auction.bidCeiling) + ' ceiling',
        note: 'surviving-lien haircut applied', noteColor: neutral },
      { model: 'Wholesale', out: 'assignment spread',
        note: 'BLOCKED — SB 1577 disclosure', noteColor: bad },
      { model: 'Subject-To', out: money(a.arv - a.purchase) + ' equity captured',
        note: 'due-on-sale risk flag', noteColor: neutral },
      { model: 'Short sale', out: 'lender-approval timeline',
        note: 'patience posture', noteColor: neutral },
    ];
  }

  // ------------------------------------------------------------- self-test
  /* Fixture from the handoff: ARV 750k / purchase 475k / rehab 50k /
     holding 5k / financing 10k, sell-side closing at 7.5% of ARV = $56,250:
       net = 750,000 − 475,000 − 60,000(rehab+20%) − 5,000 − 10,000 − 56,250
           = 143,750.
     The < $1 assertion mirrors test_recalc.py BALANCE_TOLERANCE = 1.0. */
  function fixtureAssumptions() {
    return {
      arv: 750000, purchase: 475000, rehab: 50000, rent: 3900,
      contingency: 0.20, maoFactor: 0.70, loanRate: 0.061,
      holding: 5000, financing: 10000, closing: 56250,
      confidence: 0.91, compSource: 'mls_agent',
    };
  }

  function runSelfTests() {
    const results = [];
    const chk = (name, cond) => {
      cond = !!cond;
      results.push([name, cond]);
      console.assert(cond, '[formula-engine] self-test failed: ' + name);
    };

    const fx = computeAll(fixtureAssumptions());
    chk('flip fixture |net − 143750| < 1 (parity w/ test_recalc.py tolerance)',
        Math.abs(fx.netProfit - 143750) < 1);
    chk('flip fixture MAO == 475000 (70% rule)', fx.mao === 475000);
    chk('flip fixture rehab+contingency == 60000', fx.rehabTotal === 60000);

    // Amortization parity with build_workbook.py's monthly-payment formula.
    chk('pmt($300k @ 6%, 360mo) ≈ $1,798.65', Math.abs(pmt(300000, 0.06, 360) - 1798.65) < 0.02);
    chk('pmt handles zero rate deterministically', pmt(120000, 0, 360) === 333.3333333333333 ||
        Math.abs(pmt(120000, 0, 360) - 120000 / 360) < 1e-9);

    // test_recalc.py [6] parity: premium is exactly 25% of bid; yield ~57.22%.
    chk('tax-deed redemption return == bid × 25%',
        fx.taxdeed.redemptionReturn === fx.taxdeed.bid * REDEMPTION_PREMIUM);
    chk('tax-deed annualized ≈ 57.22%', Math.abs(fx.taxdeed.annualized - 0.572216) < 0.0005);

    // Rental fixture: NOI = 3900×12×0.6 = 28,080 → cap = 28,080 / 475,000.
    chk('rental cap rate == NOI / purchase',
        Math.abs(fx.rental.capRate - (3900 * 12 * OPEX_RATIO) / 475000) < 1e-9);

    // Auction ceiling: MAO 475,000 less the 18,000 surviving-lien haircut.
    chk('auction bid ceiling == MAO − 18,000', fx.auction.bidCeiling === 457000);

    // GATE-VAL matrix (mirrors test_gate_val_verdict_is_live_and_passing).
    const g = (src, conf) =>
      computeAll(Object.assign({}, fixtureAssumptions(), { compSource: src, confidence: conf })).gate.verdict;
    chk('mls_agent conf .91 → PASS', g('mls_agent', 0.91) === 'PASS');
    chk('mls_agent conf .84 < floor .85 → FAIL', g('mls_agent', 0.84) === 'FAIL');
    chk('appraiser_bpo conf .80 ≥ floor .75 → PASS', g('appraiser_bpo', 0.80) === 'PASS');
    chk('title_feed conf .60 ≥ floor .60 → PASS', g('title_feed', 0.60) === 'PASS');
    chk('title_feed conf .59 → FAIL', g('title_feed', 0.59) === 'FAIL');
    chk('vendor_avm conf .49 < floor .50 → FAIL', g('vendor_avm', 0.49) === 'FAIL');
    chk('vendor_avm conf .60 → WARN', g('vendor_avm', 0.60) === 'WARN');
    chk('assessed_only always FAIL', g('assessed_only', 0.99) === 'FAIL');
    chk('zestimate_only always FAIL', g('zestimate_only', 0.99) === 'FAIL');

    // Sensitivity grid shape: 5 ARV rows × 4 rehab cols, center cell = base.
    const grid = sensitivityGrid(fixtureAssumptions(), fx.rehabTotal);
    chk('sensitivity grid is 5×4',
        grid.length === 5 && grid.every((r) => r.cells.length === 4));
    chk('sensitivity center row/col equals base net (in $k)',
        grid[2].cells[1].text === '$144k');

    const passed = results.filter((r) => r[1]).length;
    const summary = {
      passed,
      failed: results.length - passed,
      total: results.length,
      failures: results.filter((r) => !r[1]).map((r) => r[0]),
    };
    if (typeof window !== 'undefined') window.__UW_SELFTEST = summary;
    console.log('[formula-engine] self-test: ' + passed + '/' + results.length +
                ' checks passed' + (summary.failed ? ' — FAILURES: ' + summary.failures.join('; ') : ''));
    return summary;
  }

  // -------------------------------------------------------------- DOM glue
  const $id = (id) => document.getElementById(id);
  let dom = null;            // cached element refs
  let activeModel = 'flip';  // selected model tab
  let compSource = 'mls_agent';
  let initialSnapshot = [];  // captured once for deterministic reset
  let lastComputed = null;   // for the CSV snapshot export

  function readState() {
    const num = (el, fallback) => {
      const v = parseFloat(el.value);
      return isFinite(v) ? v : fallback;
    };
    return {
      arv: num(dom.in.arv, 0),
      purchase: num(dom.in.purchase, 0),
      rehab: num(dom.in.rehab, 0),
      rent: num(dom.in.rent, 0),
      contingency: num(dom.in.contingency, 20) / 100,
      maoFactor: num(dom.in.maoFactor, 70) / 100,
      loanRate: num(dom.in.loanRate, 6.10) / 100,
      holding: num(dom.in.holding, 0),
      financing: num(dom.in.financing, 0),
      closing: num(dom.in.closing, 0),
      confidence: num(dom.in.confidence, 0.91),
      compSource,
    };
  }

  function renderMetrics(model, c, s) {
    dom.metricsRow.innerHTML = metricDefs(model, c, s).map((m) => (
      '<div class="metric' + (m.tone !== 'plain' ? ' tint-' + m.tone : '') + '">' +
        '<div class="metric-k">' + m.label + '</div>' +
        '<div class="metric-v uw-figure tone-' + m.tone + '">' + m.value + '</div>' +
      '</div>'
    )).join('');
  }

  function renderWaterfall(a, c) {
    dom.wf.arv.textContent = money(a.arv);
    dom.wf.purchase.textContent = money(-a.purchase);
    dom.wf.rehab.textContent = money(-c.rehabTotal);
    dom.wf.cont.textContent = pctLabel(a.contingency);
    dom.wf.holding.textContent = money(-a.holding);
    dom.wf.financing.textContent = money(-a.financing);
    dom.wf.closing.textContent = money(-a.closing);
    dom.wf.net.textContent = money(c.netProfit);
    // Tone only when trusted; .gate-dim overrides via !important on FAIL.
    dom.wf.net.style.color = c.gate.untrusted ? '' : (c.netProfit >= 0 ? '#33513c' : '#8a2f22');
  }

  function renderLadder(c) {
    dom.lad.opening.textContent = money(OPEN_PCT * c.mao);
    dom.lad.target.textContent = money(c.mao);
    dom.lad.walk.textContent = money(WALK_PCT * c.mao);
    dom.lad.cost.textContent = money(c.totalCost);
  }

  function renderSensitivity(a, c) {
    dom.sensBody.innerHTML = sensitivityGrid(a, c.rehabTotal).map((row) => (
      '<tr><td class="sens-label">' + row.label + '</td>' +
      row.cells.map((cell) =>
        '<td class="sens-cell uw-figure ' + cell.cls + '">' + cell.text + '</td>'
      ).join('') + '</tr>'
    )).join('');
  }

  function renderRollup(a, c) {
    lastComputed = c;
    dom.rollupBody.innerHTML = rollupRows(a, c).map((m) => (
      '<tr><td class="uw-model-name">' + m.model + '</td>' +
      '<td class="uw-out-cell uw-figure">' + m.out + '</td>' +
      '<td class="uw-status-cell" style="color:' + m.noteColor + '">' + m.note + '</td></tr>'
    )).join('');
  }

  /** One recompute pass — reads inputs then writes every bound element. */
  function recompute() {
    const a = readState();
    const c = computeAll(a);

    // Assumption live values
    dom.out.arv.textContent = money(a.arv);
    dom.out.purchase.textContent = money(a.purchase);
    dom.out.rehab.textContent = money(a.rehab);
    dom.out.rent.textContent = money(a.rent) + '/mo';
    dom.out.conf.textContent = a.confidence.toFixed(2);

    // Verdict gate
    dom.verdictPill.textContent = c.gate.verdict.toLowerCase();
    dom.verdictPill.className = 'pill pill-' + c.gate.verdict.toLowerCase();
    dom.verdictReason.textContent = c.gate.reason;
    dom.banner.hidden = !c.gate.untrusted;
    dom.page.classList.toggle('gate-dim', c.gate.untrusted);

    // Model outputs
    renderMetrics(activeModel, c, a);
    dom.modelNote.textContent = MODEL_NOTES[activeModel] || MODEL_NOTES.flip;
    const flipOnly = activeModel === 'flip';
    dom.flipExtra.style.display = flipOnly ? '' : 'none';
    dom.sensSection.style.display = flipOnly ? '' : 'none';
    if (flipOnly) {
      renderWaterfall(a, c);
      renderLadder(c);
      renderSensitivity(a, c);
    }
    renderRollup(a, c);
  }

  /** Coalesce bursts of input events into a single animation frame. */
  let framePending = null;
  function schedule() {
    if (framePending !== null) return;
    framePending = requestAnimationFrame(() => {
      framePending = null;
      recompute();
    });
  }

  function setPressed(container, attr, value) {
    container.querySelectorAll('.uw-chip').forEach((btn) => {
      btn.setAttribute('aria-pressed', String(btn.dataset[attr] === value));
    });
  }

  function captureInitial() {
    return Object.keys(dom.in).map((k) => ({ key: k, value: dom.in[k].value }));
  }

  function resetToInitial() {
    initialSnapshot.forEach(({ key, value }) => { dom.in[key].value = value; });
    activeModel = 'flip';
    compSource = 'mls_agent';
    setPressed(dom.tabs, 'model', activeModel);
    setPressed(dom.sourceWrap, 'src', compSource);
    schedule();
  }

  function exportCsv() {
    if (!lastComputed) recompute();
    const q = (v) => '"' + String(v).replace(/"/g, '""') + '"';
    const lines = [['Deal type', 'Headline output', 'Gate / status'].map(q).join(',')]
      .concat(rollupRows(readState(), lastComputed).map((m) =>
        [q(m.model), q(m.out), q(m.note)].join(',')));
    const blob = new Blob([lines.join('\n') + '\n'], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'HERMES-REO_underwriting_4003592.csv';
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function cacheDom() {
    dom = {
      page: $id('uw-page'),
      in: {
        arv: $id('in-arv'), purchase: $id('in-purchase'), rehab: $id('in-rehab'),
        rent: $id('in-rent'), contingency: $id('in-contingency'),
        maoFactor: $id('in-mao-factor'), loanRate: $id('in-loan-rate'),
        holding: $id('in-holding'), financing: $id('in-financing'),
        closing: $id('in-closing'), confidence: $id('in-confidence'),
      },
      out: {
        arv: $id('out-arv'), purchase: $id('out-purchase'), rehab: $id('out-rehab'),
        rent: $id('out-rent'), conf: $id('out-conf'),
      },
      verdictPill: $id('verdict-pill'),
      verdictReason: $id('verdict-reason'),
      banner: $id('gate-banner'),
      metricsRow: $id('metrics-row'),
      modelNote: $id('model-note'),
      flipExtra: $id('flip-extra'),
      sensSection: $id('sens-section'),
      sensBody: $id('sens-body'),
      rollupBody: $id('rollup-body'),
      tabs: $id('model-tabs'),
      sourceWrap: $id('comp-source'),
      wf: {
        arv: $id('wf-arv'), purchase: $id('wf-purchase'), rehab: $id('wf-rehab'),
        cont: $id('wf-cont'), holding: $id('wf-holding'),
        financing: $id('wf-financing'), closing: $id('wf-closing'), net: $id('wf-net'),
      },
      lad: {
        opening: $id('lad-opening'), target: $id('lad-target'),
        walk: $id('lad-walk'), cost: $id('lad-cost'),
      },
    };
  }

  function bind() {
    Object.keys(dom.in).forEach((k) => {
      dom.in[k].addEventListener('input', schedule);
      dom.in[k].addEventListener('change', schedule);
    });
    dom.tabs.addEventListener('click', (ev) => {
      const btn = ev.target.closest('.uw-chip[data-model]');
      if (!btn) return;
      activeModel = btn.dataset.model;
      setPressed(dom.tabs, 'model', activeModel);
      schedule();
    });
    dom.sourceWrap.addEventListener('click', (ev) => {
      const btn = ev.target.closest('.uw-chip[data-src]');
      if (!btn) return;
      compSource = btn.dataset.src;
      setPressed(dom.sourceWrap, 'src', compSource);
      schedule();
    });
    $id('btn-reset').addEventListener('click', resetToInitial);
    $id('btn-export').addEventListener('click', exportCsv);
  }

  function boot() {
    cacheDom();
    bind();
    initialSnapshot = captureInitial();
    recompute();          // paint the defaults synchronously, first frame
    runSelfTests();       // console.assert parity vs test_recalc.py fixtures
  }

  // Node (verification harness) gets the pure surface; the browser boots.
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { pmt, computeAll, runSelfTests, sensitivityGrid, rollupRows };
  }
  if (typeof window !== 'undefined' && typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', boot);
    } else {
      boot();
    }
  }
})();
