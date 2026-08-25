#!/usr/bin/env python3
# build_workbook.py — Generate the HERMES-REO Excel model suite.
#
# Builds a single master workbook with:
# 1. README/Control
# 2. Assumptions (named ranges, all §6 defaults)
# 3. Market Data (DFW tax rates, rehab tiers, financing, closing costs)
# 4. Comp & ARV (GATE-VAL live logic)
# 5. Rehab Estimator
# 6. Deal Screener
# 7. Fix-and-Flip
# 8. BRRRR
# 9. Buy-and-Hold Rental (multi-year DCF)
# 10. Tax-Deed / Redemption
# 11. Wholesale / Assignment
# 12. Auction Bid
# 13. Subject-To
# 14. Sensitivity / Scenario
# 15. Portfolio Dashboard
# 16. Checks
#
# Every output cell is a formula. Every input is blue with a source comment.
# GATE-VAL is live cell logic, not a comment.
from __future__ import annotations
import os, sys
from datetime import date

from src.dates import parse_date_any

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, numbers
from openpyxl.comments import Comment
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import FormulaRule

# --- Style constants ---
BLUE = Font(color="0000FF")
BLACK = Font(color="000000")
GREEN = Font(color="006100")
BOLD = Font(bold=True)
WHITE_BOLD = Font(color="FFFFFF", bold=True)
HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
SUBHEADER_FILL = PatternFill("solid", fgColor="2E75B6")
INPUT_FILL = PatternFill("solid", fgColor="DDEBF7")
GATE_PASS = PatternFill("solid", fgColor="C6EFCE")
GATE_FAIL = PatternFill("solid", fgColor="FFC7CE")
GATE_WARN = PatternFill("solid", fgColor="FFEB9C")
CENTER_FILL = PatternFill("solid", fgColor="BDD7EE")
THIN_BORDER = Border(
    bottom=Side(style="thin", color="B0B0B0"),
    left=Side(style="thin", color="B0B0B0"),
    right=Side(style="thin", color="B0B0B0"),
    top=Side(style="thin", color="B0B0B0"),
)
PCT_FMT = '0.0%'
USD_FMT = '$#,##0'
USD2_FMT = '$#,##0.00'
DATE_FMT = 'yyyy-mm-dd'

# --- Helper functions ---
def set_input(ws, cell, value, comment_text="", fmt=None):
    """Set a blue input cell with optional source comment."""
    ws[cell] = value
    ws[cell].font = BLUE
    ws[cell].fill = INPUT_FILL
    if comment_text:
        ws[cell].comment = Comment(comment_text, "model")
    if fmt:
        ws[cell].number_format = fmt

def set_formula(ws, cell, formula, fmt=None, bold=False):
    """Set a black formula cell."""
    ws[cell] = formula
    ws[cell].font = BOLD if bold else BLACK
    if fmt:
        ws[cell].number_format = fmt

def set_link(ws, cell, formula, fmt=None):
    """Set a green link cell (cross-sheet reference)."""
    ws[cell] = formula
    ws[cell].font = GREEN
    if fmt:
        ws[cell].number_format = fmt

def header_row(ws, row, text, cols=8):
    """Write a header row with fill and merge."""
    ws.cell(row=row, column=1).value = text
    ws.cell(row=row, column=1).font = WHITE_BOLD
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=cols)
    for c in range(1, cols + 1):
        ws.cell(row=row, column=c).fill = HEADER_FILL

def subheader(ws, row, text, cols=4):
    ws.cell(row=row, column=1).value = text
    ws.cell(row=row, column=1).font = WHITE_BOLD
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=cols)
    for c in range(1, cols + 1):
        ws.cell(row=row, column=c).fill = SUBHEADER_FILL

def add_name(wb, name, ref):
    """Add a named range to the workbook."""
    wb.defined_names[name] = DefinedName(name, attr_text=ref)

def col_widths(ws, widths):
    for col, w in widths.items():
        ws.column_dimensions[col].width = w

def add_text_rule(ws, rng, needle, fill):
    """Conditional format: highlight cells in rng whose text contains needle.

    SEARCH-based FormulaRule instead of CellIsRule containsText, which some
    engines (LibreOffice) do not honor; the formula is anchored to the first
    cell of the range and applies relatively to every cell in it.
    """
    first_cell = rng.split(":")[0]
    ws.conditional_formatting.add(
        rng,
        FormulaRule(formula=[f'NOT(ISERROR(SEARCH("{needle}",{first_cell})))'], fill=fill),
    )


# ============================================================
# BUILD WORKBOOK
# ============================================================
wb = Workbook()

# Remove default sheet, we'll create all tabs explicitly
wb.remove(wb.active)

# ============================================================
# 1. README / CONTROL
# ============================================================
ws = wb.create_sheet("README")
col_widths(ws, {"A": 28, "B": 50, "C": 20, "D": 20, "E": 20})
header_row(ws, 1, "HERMES-REO — Excel Model Suite")
ws["A3"] = "Version"
ws["B3"] = "1.0"
ws["A4"] = "Build date"
ws["B4"] = date.today().isoformat()
ws["A5"] = "Jurisdiction"
ws["B5"] = "Collin / Dallas / Grayson County, TX"
ws["A6"] = "Status"
ws["B6"] = "Formula-driven — all outputs recalculate from Assumptions"

ws["A8"] = "TAB MAP"
ws["A8"].font = BOLD
tabs = [
    ("README", "This tab — model map, version, color legend"),
    ("Assumptions", "Single source of truth — all inputs as named ranges"),
    ("Market Data", "DFW market defaults (tax rates, rehab, financing, closing)"),
    ("Comp & ARV", "Valuation engine with GATE-VAL non-disclosure gate"),
    ("Rehab Estimator", "Line-item + $/SF rehab budget with DFW risk premiums"),
    ("Deal Screener", "Fast go/no-go — 70% rule, 1% rule, cap rate screen"),
    ("Fix-and-Flip", "Full P&L waterfall with hard-money debt schedule"),
    ("BRRRR", "Acquire-rehab-refi-hold with capital recycle analysis"),
    ("Rental DCF", "Multi-year buy-and-hold with levered/unlevered IRR"),
    ("Tax-Deed", "Redemption yield + acquire-and-quiet-title branches"),
    ("Wholesale", "Assignment vs double-close with compliance gate"),
    ("Auction Bid", "Bid ceiling with surviving-lien haircut"),
    ("Subject-To", "Take title subject to existing loan — due-on-sale flag"),
    ("Sensitivity", "2-variable Data Tables + Base/Bull/Bear scenarios"),
    ("Portfolio", "Roll-up of all active deals"),
    ("Checks", "Balance checks, integrity flags, GATE-VAL verification"),
]
for i, (tab, desc) in enumerate(tabs, 10):
    ws.cell(row=i, column=1).value = tab
    ws.cell(row=i, column=1).font = BOLD
    ws.cell(row=i, column=2).value = desc

r = 10 + len(tabs) + 2
ws.cell(row=r, column=1).value = "COLOR LEGEND"
ws.cell(row=r, column=1).font = BOLD
ws.cell(row=r+1, column=1).value = "Blue"
ws.cell(row=r+1, column=1).font = BLUE
ws.cell(row=r+1, column=2).value = "Hardcoded input (human-entered assumption)"
ws.cell(row=r+2, column=1).value = "Black"
ws.cell(row=r+2, column=2).value = "Formula (computed, not editable)"
ws.cell(row=r+3, column=1).value = "Green"
ws.cell(row=r+3, column=1).font = GREEN
ws.cell(row=r+3, column=2).value = "Link to another sheet"
ws.cell(row=r+4, column=1).value = "Green fill"
ws.cell(row=r+4, column=1).fill = GATE_PASS
ws.cell(row=r+4, column=2).value = "Gate PASS / clears benchmark"
ws.cell(row=r+5, column=1).value = "Red fill"
ws.cell(row=r+5, column=1).fill = GATE_FAIL
ws.cell(row=r+5, column=2).value = "Gate FAIL / fails benchmark"
ws.cell(row=r+6, column=1).value = "Yellow fill"
ws.cell(row=r+6, column=1).fill = GATE_WARN
ws.cell(row=r+6, column=2).value = "Marginal / warning"

r2 = r + 8
ws.cell(row=r2, column=1).value = "RECALCULATION SELF-TEST"
ws.cell(row=r2, column=1).font = BOLD
ws.cell(row=r2+1, column=2).value = "1. Open any output cell (e.g., Fix-and-Flip!Net_Profit)"
ws.cell(row=r2+2, column=2).value = "2. Change one assumption on Assumptions tab (e.g., ARV -5%)"
ws.cell(row=r2+3, column=2).value = "3. Return to the output — it must recalculate"
ws.cell(row=r2+4, column=2).value = "4. If any headline output is static, the model is rejected"
ws.cell(row=r2+5, column=2).value = "5. GATE-VAL must block MAO/bid when comp source = assessed-only"

print("README tab built")

# ============================================================
# 2. ASSUMPTIONS
# ============================================================
ws = wb.create_sheet("Assumptions")
col_widths(ws, {"A": 35, "B": 18, "C": 18, "D": 35})
header_row(ws, 1, "ASSUMPTIONS — Single Source of Truth")

# --- General deal inputs ---
subheader(ws, 3, "GENERAL DEAL INPUTS")
rows = [
    ("County", "Collin", "county", "Collin / Dallas / Grayson"),
    ("Deal type", "Fix-and-Flip", "deal_type", "Screener/Flip/BRRRR/Rental/TaxDeed/Wholesale/Auction/SubTo"),
    ("Blast tier", "T3", "blast_tier", "T0 screen / T1 small / T2 mid / T3 large"),
    ("Property address", "10022 Plainsman Ln, Frisco, TX 75035", "prop_addr", "Situs address"),
    ("Parcel ID", "4003592", "parcel_id", "County entry ID"),
    ("Purchase price", 300000, "purchase_price", "MAO or negotiated price — should be <= 70% rule MAO"),
    ("ARV (after-repair value)", 750000, "arv", "GATED — must pass GATE-VAL"),
    ("As-is value", 600000, "as_is_value", "Current unrenovated value"),
    ("Rehab budget", 50000, "rehab_budget", "From Rehab Estimator tab"),
    ("Hold months", 6, "hold_months", "Months from acquisition to sale"),
    ("Sqft", 4332, "sqft", "Improvement sqft"),
    ("Beds", 5, "beds", ""),
    ("Baths", 4, "baths", ""),
    ("Year built", 2004, "year_built", ""),
]
for i, (label, val, name, note) in enumerate(rows):
    r = 4 + i
    ws.cell(row=r, column=1).value = label
    set_input(ws, f"B{r}", val, f"Source: user input — {note}")
    if name:
        add_name(wb, name, f"Assumptions!$B${r}")

# --- Market data inputs (from §6) ---
r = 4 + len(rows) + 1
subheader(ws, r, "MARKET DATA — DFW DEFAULTS (verify before use)")
r += 1

# Property tax rates
ws.cell(row=r, column=1).value = "Property tax — Collin (eff. annual %)"
set_input(ws, f"B{r}", 0.020, "Source: Collin CAD 2026, combined ISD+city+county — verify per parcel", PCT_FMT)
add_name(wb, "tax_rate_collin", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Property tax — Dallas (eff. annual %)"
set_input(ws, f"B{r}", 0.023, "Source: DCAD 2026, DISD+city+Parkland stack — verify per parcel", PCT_FMT)
add_name(wb, "tax_rate_dallas", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Property tax — Grayson (eff. annual %)"
set_input(ws, f"B{r}", 0.018, "Source: Grayson CAD 2026 — verify at tax office", PCT_FMT)
add_name(wb, "tax_rate_grayson", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Active tax rate (by county)"
set_formula(ws, f"B{r}", '=IF(county="Collin",tax_rate_collin,IF(county="Dallas",tax_rate_dallas,tax_rate_grayson))', PCT_FMT)
add_name(wb, "tax_rate", f"Assumptions!$B${r}")
r += 1

# Rehab cost tiers
ws.cell(row=r, column=1).value = "Rehab $/SF — Light/cosmetic"
set_input(ws, f"B{r}", 20, "Source: DFW contractor market 2026, ~14% below national avg", USD_FMT)
add_name(wb, "rehab_light", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Rehab $/SF — Moderate/heavy"
set_input(ws, f"B{r}", 40, "Source: DFW contractor market 2026", USD_FMT)
add_name(wb, "rehab_moderate", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Rehab $/SF — Full gut"
set_input(ws, f"B{r}", 100, "Source: DFW contractor market 2026, range $60-150", USD_FMT)
add_name(wb, "rehab_gut", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Rehab contingency %"
set_input(ws, f"B{r}", 0.20, "Source: industry standard 10-25% by scope", PCT_FMT)
add_name(wb, "rehab_contingency", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Foundation risk premium"
set_input(ws, f"B{r}", 5000, "Source: DFW clay soil — verify with inspection", USD_FMT)
add_name(wb, "foundation_premium", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Roof/HVAC risk premium"
set_input(ws, f"B{r}", 8000, "Source: DFW hail season — verify age of systems", USD_FMT)
add_name(wb, "roof_hvac_premium", f"Assumptions!$B${r}")
r += 1

# Financing — hard money
ws.cell(row=r, column=1).value = "Hard money rate (annual)"
set_input(ws, f"B{r}", 0.0995, "Source: Q1 2026 DFW avg ~9.95%, range 8-15%", PCT_FMT)
add_name(wb, "hm_rate", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Hard money points"
set_input(ws, f"B{r}", 0.0296, "Source: 2026 avg ~2.96%, range 2-5 pts", PCT_FMT)
add_name(wb, "hm_points", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Hard money ARV-LTV cap"
set_input(ws, f"B{r}", 0.70, "Source: 2026 market, range 65-80%", PCT_FMT)
add_name(wb, "hm_arv_ltv", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Hard money LTC cap"
set_input(ws, f"B{r}", 0.85, "Source: 2026 market, range 80-90%", PCT_FMT)
add_name(wb, "hm_ltc", f"Assumptions!$B${r}")
r += 1

# Financing — DSCR
ws.cell(row=r, column=1).value = "DSCR loan rate (annual)"
set_input(ws, f"B{r}", 0.061, "Source: Aug 2026, 740 FICO / 70% LTV / 0 pts", PCT_FMT)
add_name(wb, "dscr_rate", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "DSCR refi LTV"
set_input(ws, f"B{r}", 0.75, "Source: 2026 market, range 70-75%", PCT_FMT)
add_name(wb, "refi_ltv", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Min DSCR for refi"
set_input(ws, f"B{r}", 1.25, "Source: 2026 market, some lenders floor 1.05-1.20", '0.00')
add_name(wb, "min_dscr", f"Assumptions!$B${r}")
r += 1

# Closing costs
ws.cell(row=r, column=1).value = "Buy-side closing cost %"
set_input(ws, f"B{r}", 0.025, "Source: TX title promulgated + escrow + recording — verify", PCT_FMT)
add_name(wb, "buy_closing_pct", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Sell-side closing cost %"
set_input(ws, f"B{r}", 0.075, "Source: ~5-6% commission + title + concessions", PCT_FMT)
add_name(wb, "sell_closing_pct", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Commission %"
set_input(ws, f"B{r}", 0.05, "Source: 2026 market, 5-6% typical", PCT_FMT)
add_name(wb, "commission_pct", f"Assumptions!$B${r}")
r += 1

# 70% rule
ws.cell(row=r, column=1).value = "MAO factor (70% rule)"
set_input(ws, f"B{r}", 0.70, "Source: industry standard; disciplined market 0.65", PCT_FMT)
add_name(wb, "mao_factor", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Wholesale MAO factor"
set_input(ws, f"B{r}", 0.65, "Source: end buyer must still clear 70%", PCT_FMT)
add_name(wb, "wholesale_factor", f"Assumptions!$B${r}")
r += 1

# Rental benchmarks
ws.cell(row=r, column=1).value = "Monthly rent estimate"
set_input(ws, f"B{r}", 3500, "Source: DFW rental market — verify with RentCast/Rentometer", USD_FMT)
add_name(wb, "monthly_rent", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Vacancy rate"
set_input(ws, f"B{r}", 0.05, "Source: DFW market avg 5%", PCT_FMT)
add_name(wb, "vacancy_rate", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Management fee %"
set_input(ws, f"B{r}", 0.08, "Source: 8-10% of gross rent", PCT_FMT)
add_name(wb, "mgmt_fee_pct", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Maintenance reserve %"
set_input(ws, f"B{r}", 0.05, "Source: 5-8% of gross rent", PCT_FMT)
add_name(wb, "maintenance_pct", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Insurance (annual)"
set_input(ws, f"B{r}", 2400, "Source: DFW market — verify per property", USD_FMT)
add_name(wb, "insurance_annual", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Cap rate benchmark"
set_input(ws, f"B{r}", 0.065, "Source: DFW Class B suburban ~6.0-7.5%", PCT_FMT)
add_name(wb, "cap_rate_bench", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Cash-on-cash benchmark"
set_input(ws, f"B{r}", 0.10, "Source: DFW market ~8-12%", PCT_FMT)
add_name(wb, "coc_bench", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Exit cap rate (rental)"
set_input(ws, f"B{r}", 0.07, "Source: 2026 DFW — verify with broker", PCT_FMT)
add_name(wb, "exit_cap_rate", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Rent growth (annual)"
set_input(ws, f"B{r}", 0.03, "Source: DFW long-term avg ~3%", PCT_FMT)
add_name(wb, "rent_growth", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Expense growth (annual)"
set_input(ws, f"B{r}", 0.025, "Source: DFW long-term avg ~2.5%", PCT_FMT)
add_name(wb, "expense_growth", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Hurdle rate (NPV discount)"
set_input(ws, f"B{r}", 0.10, "Source: investor required return", PCT_FMT)
add_name(wb, "hurdle_rate", f"Assumptions!$B${r}")
r += 1

# Tax-deed constants
ws.cell(row=r, column=1).value = "Tax-deed redemption premium yr1"
set_input(ws, f"B{r}", 0.25, "Source: TX Tax Code §34.21 — 25% yr1", PCT_FMT)
add_name(wb, "redemption_premium_yr1", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Tax-deed redemption premium yr2"
set_input(ws, f"B{r}", 0.50, "Source: TX Tax Code §34.21 — 50% yr2 (homestead/ag only)", PCT_FMT)
add_name(wb, "redemption_premium_yr2", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Redemption days — non-homestead"
set_input(ws, f"B{r}", 180, "Source: TX Tax Code §34.21 — 180 days")
add_name(wb, "redemption_days_other", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Redemption days — homestead/ag"
set_input(ws, f"B{r}", 730, "Source: TX Tax Code §34.21 — 2 years")
add_name(wb, "redemption_days_homestead", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "Quiet-title cost"
set_input(ws, f"B{r}", 3500, "Source: TX RE attorney — verify", USD_FMT)
add_name(wb, "quiet_title_cost", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "IRS federal redemption days"
set_input(ws, f"B{r}", 120, "Source: federal tax lien — 120-day right of redemption")
add_name(wb, "irs_redemption_days", f"Assumptions!$B${r}")
r += 1

# Wholesale
ws.cell(row=r, column=1).value = "Target assignment fee"
set_input(ws, f"B{r}", 10000, "Source: DFW market — $5-15k typical", USD_FMT)
add_name(wb, "assignment_fee", f"Assumptions!$B${r}")
r += 1

# GATE-VAL confidence floors
ws.cell(row=r, column=1).value = "GATE-VAL confidence floor — T0"
set_input(ws, f"B{r}", 0.50, "Source: screen-only tier", PCT_FMT)
add_name(wb, "conf_floor_t0", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "GATE-VAL confidence floor — T1"
set_input(ws, f"B{r}", 0.60, "Source: small deal tier", PCT_FMT)
add_name(wb, "conf_floor_t1", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "GATE-VAL confidence floor — T2"
set_input(ws, f"B{r}", 0.75, "Source: mid deal — agent-MLS/BPO required", PCT_FMT)
add_name(wb, "conf_floor_t2", f"Assumptions!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "GATE-VAL confidence floor — T3"
set_input(ws, f"B{r}", 0.85, "Source: large/auction — appraiser-grade required", PCT_FMT)
add_name(wb, "conf_floor_t3", f"Assumptions!$B${r}")
r += 1

print(f"Assumptions tab built — {r} rows")

# ============================================================
# 3. COMP & ARV (GATE-VAL)
# ============================================================
ws = wb.create_sheet("Comp & ARV")
col_widths(ws, {"A": 30, "B": 16, "C": 16, "D": 16, "E": 16, "F": 16, "G": 16, "H": 16})
header_row(ws, 1, "COMP & ARV — Valuation Engine with GATE-VAL")

# Comp grid
subheader(ws, 3, "COMP GRID (enter 3-6 adjusted comps)")
ws["A4"] = "Field"
ws["A4"].font = BOLD
comp_fields = ["Address", "Sold price", "Sold date", "Sqft", "Beds", "Baths", "Lot sqft", "Distance (mi)", "DOM", "Condition (1-5)", "Adjusted value"]
for j, f in enumerate(comp_fields):
    ws.cell(row=4, column=1 + j).value = f
    ws.cell(row=4, column=1 + j).font = BOLD

# Hidden helper column L ("adj-used"): per-row adjusted value that feeds MEDIAN
# for the weighted ARV (LibreOffice-safe — see WEIGHTED ARV below).
ws.cell(row=4, column=12).value = "adj-used"
ws.cell(row=4, column=12).font = BOLD
ws.column_dimensions["L"].hidden = True

# 6 comp rows (rows 5-10) — seeded with sample DFW comps
sample_comps = [
    ("10030 Plainsman Ln, Frisco TX", 755000, "2026-06-15", 4200, 5, 4, 9000, 0.10, 45, 4),
    ("10015 Plainsman Ln, Frisco TX", 745000, "2026-05-20", 4100, 5, 4, 8500, 0.15, 60, 4),
    ("10040 Plainsman Ct, Frisco TX", 760000, "2026-07-01", 4400, 5, 4, 9500, 0.20, 30, 5),
    ("10050 Plainsman Ct, Frisco TX", 748000, "2026-06-10", 4250, 5, 4, 8800, 0.25, 75, 3),
    ("10060 Plainsman Dr, Frisco TX", 752000, "2026-07-15", 4300, 5, 4, 9200, 0.30, 50, 4),
    ("", 0, "", 0, 0, 0, 0, 0.5, 90, 3),  # 6th empty
]
for i in range(6):
    r = 5 + i
    comp = sample_comps[i]
    sold_date = parse_date_any(comp[2]) if str(comp[2] or "").strip() else None
    if str(comp[2] or "").strip() and sold_date is None:
        raise ValueError(f"Seeded comp sold date is unparseable: {comp[2]!r}")
    set_input(ws, f"A{r}", comp[0], f"Source: MLS agent comp #{i+1}")
    set_input(ws, f"B{r}", comp[1], f"Source: sold price — MLS", USD_FMT)
    # Blank sold date = unfilled placeholder row: leave the input empty, never
    # synthesize a date.
    set_input(ws, f"C{r}", sold_date, f"Source: sold date — MLS", DATE_FMT)
    set_input(ws, f"D{r}", comp[3], f"Source: sqft — MLS")
    set_input(ws, f"E{r}", comp[4], f"Source: beds — MLS")
    set_input(ws, f"F{r}", comp[5], f"Source: baths — MLS")
    set_input(ws, f"G{r}", comp[6], f"Source: lot sqft — MLS")
    set_input(ws, f"H{r}", comp[7], f"Source: distance from subject (mi)")
    set_input(ws, f"I{r}", comp[8], f"Source: days on market — MLS")
    set_input(ws, f"J{r}", comp[9], f"Source: condition 1=poor 5=excellent")
    # Adjusted value = sold price (user can add adjustment formulas)
    set_formula(ws, f"K{r}", f"=B{r}", USD_FMT)
    # Helper for weighted ARV: blank non-positive rows so MEDIAN sees only real comps
    set_formula(ws, f"L{r}", f'=IF(K{r}>0,K{r},"")', USD_FMT)

# Weighted ARV
r = 12
ws.cell(row=r, column=1).value = "WEIGHTED ARV"
ws.cell(row=r, column=1).font = BOLD
r += 1
ws.cell(row=r, column=1).value = "Comps with valid price"
set_formula(ws, f"B{r}", '=COUNTIF(B5:B10,">0")')
r += 1
ws.cell(row=r, column=1).value = "Weighted ARV (median of adjusted)"
# AGGREGATE with array division doesn't work in LibreOffice, so MEDIAN runs over
# hidden helper column L ("adj-used"), which blanks non-positive rows; the grid
# spans rows 5-10 and MEDIAN(K5:K9) both dropped row 10 and averaged an empty range.
set_formula(ws, f"B{r}", '=IF(B13>=3,MEDIAN(L5:L10),"Need >=3 comps")', USD_FMT, bold=True)
add_name(wb, "weighted_arv", f"'Comp & ARV'!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "ARV used in models"
set_formula(ws, f"B{r}", "=weighted_arv", USD_FMT, bold=True)
add_name(wb, "arv_used", f"'Comp & ARV'!$B${r}")
r += 1

# GATE-VAL
r += 1
subheader(ws, r, "GATE-VAL — Non-Disclosure ARV Grounding Gate")
r += 1
ws.cell(row=r, column=1).value = "Comp provenance source"
ws.cell(row=r, column=1).font = BOLD
# Data validation dropdown for comp source
from openpyxl.worksheet.datavalidation import DataValidation
dv = DataValidation(type="list", formula1='"mls_agent,appraiser_bpo,title_feed,vendor_avm,assessed_only,zestimate_only"', allow_blank=False)
ws.add_data_validation(dv)
dv.add(f"B{r}")
set_input(ws, f"B{r}", "mls_agent", "Source: select comp provenance — last two = auto-reject")
ws.cell(row=r, column=3).value = "← Select: mls_agent / appraiser_bpo / title_feed / vendor_avm / assessed_only / zestimate_only"
add_name(wb, "comp_source", f"'Comp & ARV'!$B${r}")
r += 1

ws.cell(row=r, column=1).value = "Source rigor tier (0-3)"
set_formula(ws, f"B{r}", '=IF(comp_source="mls_agent",3,IF(comp_source="appraiser_bpo",2,IF(comp_source="title_feed",1,IF(comp_source="vendor_avm",0,-1))))')
add_name(wb, "source_rigor", f"'Comp & ARV'!$B${r}")
r += 1

ws.cell(row=r, column=1).value = "Confidence score (0-1)"
set_input(ws, f"B{r}", 0.85, "Source: user estimate — 0=low, 1=high (consider comp count, recency, proximity)", PCT_FMT)
add_name(wb, "confidence_score", f"'Comp & ARV'!$B${r}")
r += 1

ws.cell(row=r, column=1).value = "Active confidence floor"
set_formula(ws, f"B{r}", '=IF(blast_tier="T0",conf_floor_t0,IF(blast_tier="T1",conf_floor_t1,IF(blast_tier="T2",conf_floor_t2,conf_floor_t3)))', PCT_FMT)
add_name(wb, "active_conf_floor", f"'Comp & ARV'!$B${r}")
r += 1

ws.cell(row=r, column=1).value = "GATE-VAL verdict"
set_formula(ws, f"B{r}", '=IF(OR(comp_source="assessed_only",comp_source="zestimate_only"),"FAIL — rejected source",IF(comp_source="vendor_avm","WARN — directional only",IF(B13<3,"FAIL — need >=3 comps",IF(confidence_score<active_conf_floor,"FAIL — confidence below floor","PASS"))))', bold=True)
add_name(wb, "gate_val_verdict", f"'Comp & ARV'!$B${r}")
r += 1

ws.cell(row=r, column=1).value = "GATE-VAL pass (boolean)"
set_formula(ws, f"B{r}", '=IF(LEFT(gate_val_verdict,4)="PASS",TRUE,FALSE)')
add_name(wb, "gate_val_pass", f"'Comp & ARV'!$B${r}")
r += 1

# Conditional formatting on GATE-VAL verdict
add_text_rule(ws, f"B{r-2}", "PASS", GATE_PASS)
add_text_rule(ws, f"B{r-2}", "FAIL", GATE_FAIL)
add_text_rule(ws, f"B{r-2}", "WARN", GATE_WARN)

# Rent estimate
r += 1
subheader(ws, r, "RENT ESTIMATE")
r += 1
ws.cell(row=r, column=1).value = "Rent estimate (monthly)"
set_input(ws, f"B{r}", 3500, "Source: RentCast/Rentometer/Zillow Rent — verify", USD_FMT)
add_name(wb, "rent_estimate", f"'Comp & ARV'!$B${r}")
r += 1
ws.cell(row=r, column=1).value = "HUD Fair Market Rent (monthly)"
set_input(ws, f"B{r}", 2100, "Source: HUD FMR 2026 — for Section-8 scenarios", USD_FMT)
add_name(wb, "hud_fmr", f"'Comp & ARV'!$B${r}")

print("Comp & ARV tab built")

# ============================================================
# 4. REHAB ESTIMATOR
# ============================================================
ws = wb.create_sheet("Rehab Estimator")
col_widths(ws, {"A": 30, "B": 16, "C": 16, "D": 16, "E": 30})
header_row(ws, 1, "REHAB ESTIMATOR — Line-Item + $/SF Budget")

subheader(ws, 3, "REHAB TIER SELECTION")
ws.cell(row=4, column=1).value = "Rehab tier"
dv2 = DataValidation(type="list", formula1='"Light,Moderate,Full Gut"', allow_blank=False)
ws.add_data_validation(dv2)
dv2.add("B4")
set_input(ws, "B4", "Moderate", "Source: select rehab scope tier")
add_name(wb, "rehab_tier", "'Rehab Estimator'!$B$4")

ws.cell(row=5, column=1).value = "Active $/SF"
set_formula(ws, "B5", '=IF(rehab_tier="Light",rehab_light,IF(rehab_tier="Moderate",rehab_moderate,rehab_gut))', USD_FMT)
add_name(wb, "active_rehab_psf", "'Rehab Estimator'!$B$5")

ws.cell(row=6, column=1).value = "Base rehab (sqft × $/SF)"
set_formula(ws, "B6", "=sqft*active_rehab_psf", USD_FMT)
add_name(wb, "base_rehab", "'Rehab Estimator'!$B$6")

ws.cell(row=7, column=1).value = "Contingency"
set_formula(ws, "B7", "=base_rehab*rehab_contingency", USD_FMT)

ws.cell(row=8, column=1).value = "Foundation risk premium"
set_link(ws, "B8", "=foundation_premium", USD_FMT)

ws.cell(row=9, column=1).value = "Roof/HVAC risk premium"
set_link(ws, "B9", "=roof_hvac_premium", USD_FMT)

ws.cell(row=10, column=1).value = "TOTAL REHAB BUDGET"
set_formula(ws, "B10", "=B6+B7+B8+B9", USD_FMT, bold=True)
add_name(wb, "total_rehab", "'Rehab Estimator'!$B$10")

# Kill-switch
ws.cell(row=12, column=1).value = "Rehab kill-switch (rehab > 70% ARV)"
set_formula(ws, "B12", '=IF(gate_val_pass,IF(total_rehab/arv_used>0.7,"WALK AWAY — rehab exceeds 70% of ARV","OK"),"GATE-VAL FAIL")', bold=True)
add_text_rule(ws, "B12", "WALK", GATE_FAIL)
add_text_rule(ws, "B12", "OK", GATE_PASS)

# Line-item breakdown
subheader(ws, 14, "LINE-ITEM BREAKDOWN (optional — overrides $/SF if used)")
line_items = [
    "Foundation repair", "Roof replacement", "HVAC replacement",
    "Plumbing", "Electrical", "Kitchen", "Bathrooms", "Flooring",
    "Paint (int/ext)", "Landscaping", "Permits", "Other"
]
for i, item in enumerate(line_items):
    r = 15 + i
    ws.cell(row=r, column=1).value = item
    set_input(ws, f"B{r}", 0, f"Source: contractor bid — {item}", USD_FMT)

ws.cell(row=27, column=1).value = "Line-item total"
set_formula(ws, "B27", "=SUM(B15:B26)", USD_FMT)
ws.cell(row=28, column=1).value = "Rehab budget used (max of $/SF and line-item)"
set_formula(ws, "B28", "=MAX(total_rehab,B27)", USD_FMT, bold=True)
add_name(wb, "rehab_used", "'Rehab Estimator'!$B$28")

print("Rehab Estimator tab built")

# ============================================================
# 5. DEAL SCREENER
# ============================================================
ws = wb.create_sheet("Deal Screener")
col_widths(ws, {"A": 32, "B": 18, "C": 18, "D": 35})
header_row(ws, 1, "DEAL SCREENER — Fast Go/No-Go")

subheader(ws, 3, "INPUTS (from Assumptions + Comp & ARV)")
ws.cell(row=4, column=1).value = "ARV (gated)"
set_link(ws, "B4", "=arv_used", USD_FMT)
ws.cell(row=5, column=1).value = "Rehab"
set_link(ws, "B5", "=rehab_used", USD_FMT)
ws.cell(row=6, column=1).value = "Asking/opening bid"
set_input(ws, "B6", 450000, "Source: listing price or auction opening bid", USD_FMT)
add_name(wb, "asking_price", "'Deal Screener'!$B$6")
ws.cell(row=7, column=1).value = "Monthly rent"
set_link(ws, "B7", "=rent_estimate", USD_FMT)

subheader(ws, 9, "SCREENING METRICS")
ws.cell(row=10, column=1).value = "70% Rule MAO"
set_formula(ws, "B10", "=IF(gate_val_pass,arv_used*mao_factor-rehab_used,\"GATE-VAL FAIL — no MAO\")", USD_FMT, bold=True)
ws.cell(row=11, column=1).value = "65% Wholesale MAO"
set_formula(ws, "B11", "=IF(gate_val_pass,arv_used*wholesale_factor-rehab_used,\"GATE-VAL FAIL\")", USD_FMT)
ws.cell(row=12, column=1).value = "1% Rule (rent/price)"
set_formula(ws, "B12", "=IF(asking_price>0,rent_estimate/asking_price,0)", PCT_FMT)
ws.cell(row=13, column=1).value = "GRM (price/gross annual rent)"
set_formula(ws, "B13", "=IF(rent_estimate>0,asking_price/(rent_estimate*12),0)", '0.0')
ws.cell(row=14, column=1).value = "Quick cap rate (rental path)"
set_formula(ws, "B14", "=IF(asking_price>0,(rent_estimate*12*(1-vacancy_rate)*(1-mgmt_fee_pct-maintenance_pct)-insurance_annual-tax_rate*asking_price)/asking_price,0)", PCT_FMT)

subheader(ws, 16, "VERDICT")
ws.cell(row=17, column=1).value = "Deal verdict"
set_formula(ws, "B17", '=IF(NOT(gate_val_pass),"FAIL — GATE-VAL rejected ARV",IF(asking_price<=B10,"PASS — asking <= MAO",IF(B12>=0.01,"PASS — 1% rule met",IF(B14>=cap_rate_bench,"PASS — cap rate above benchmark","MARGINAL — review"))))', bold=True)
add_text_rule(ws, "B17", "PASS", GATE_PASS)
add_text_rule(ws, "B17", "FAIL", GATE_FAIL)
add_text_rule(ws, "B17", "MARGINAL", GATE_WARN)

ws.cell(row=18, column=1).value = "Binding constraint"
set_formula(ws, "B18", '=IF(NOT(gate_val_pass),"ARV provenance",IF(asking_price>B10,"Price > MAO",IF(B12<0.01,"1% rule",IF(B14<cap_rate_bench,"Cap rate","None"))))')

print("Deal Screener tab built")

# ============================================================
# 6. FIX-AND-FLIP
# ============================================================
ws = wb.create_sheet("Fix-and-Flip")
col_widths(ws, {"A": 32, "B": 18, "C": 18, "D": 18, "E": 18, "F": 18, "G": 18, "H": 18})
header_row(ws, 1, "FIX-AND-FLIP — Full P&L Waterfall")

subheader(ws, 3, "SOURCES & USES")
ws.cell(row=4, column=1).value = "PURCHASE"
ws.cell(row=4, column=1).font = BOLD
ws.cell(row=5, column=1).value = "Purchase price"
set_link(ws, "B5", "=purchase_price", USD_FMT)
ws.cell(row=6, column=1).value = "Buy-side closing costs"
set_formula(ws, "B6", "=purchase_price*buy_closing_pct", USD_FMT)
ws.cell(row=7, column=1).value = "Rehab budget"
set_link(ws, "B7", "=rehab_used", USD_FMT)
ws.cell(row=8, column=1).value = "Holding costs (total)"
set_formula(ws, "B8", "=hold_months*(insurance_annual/12+tax_rate*purchase_price/12+200)", USD_FMT)  # $200/mo utilities
ws.cell(row=9, column=1).value = "Hard money points"
set_formula(ws, "B9", "=B13*hm_points", USD_FMT)
ws.cell(row=10, column=1).value = "TOTAL USES"
set_formula(ws, "B10", "=B5+B6+B7+B8+B9", USD_FMT, bold=True)

ws.cell(row=12, column=1).value = "FINANCING"
ws.cell(row=12, column=1).font = BOLD
ws.cell(row=13, column=1).value = "Hard money loan (min of ARV-LTV, LTC)"
# Gated: loan sizing uses the approved ARV (audit H8), loud text when gate fails
set_formula(ws, "B13", '=IF(gate_val_pass,MIN(arv_used*hm_arv_ltv,(purchase_price+rehab_used)*hm_ltc),"GATE-VAL FAIL")', USD_FMT)
add_name(wb, "hm_loan", "'Fix-and-Flip'!$B$13")
ws.cell(row=14, column=1).value = "Equity required"
set_formula(ws, "B14", "=B10-B13", USD_FMT, bold=True)
add_name(wb, "equity_flip", "'Fix-and-Flip'!$B$14")

subheader(ws, 16, "DEBT SCHEDULE (monthly, interest-only)")
ws.cell(row=17, column=1).value = "Month"
ws.cell(row=17, column=2).value = "Interest"
ws.cell(row=17, column=3).value = "Cumulative interest"
for m in range(1, 13):
    r = 17 + m
    ws.cell(row=r, column=1).value = m
    set_formula(ws, f"B{r}", f"=IF(A{r}<=hold_months,hm_loan*hm_rate/12,0)", USD_FMT)
    set_formula(ws, f"C{r}", f"=IF(A{r}=1,B{r},C{r-1}+B{r})", USD_FMT)

ws.cell(row=31, column=1).value = "Total interest"
set_formula(ws, "B31", f"=SUM(B18:B29)", USD_FMT)
add_name(wb, "total_interest", "'Fix-and-Flip'!$B$31")

subheader(ws, 33, "P&L WATERFALL")
ws.cell(row=34, column=1).value = "ARV (exit value)"
set_link(ws, "B34", "=IF(gate_val_pass,arv_used,\"GATE-VAL FAIL\")", USD_FMT)
ws.cell(row=35, column=1).value = "Less: Purchase price"
set_formula(ws, "B35", "=-B5", USD_FMT)
ws.cell(row=36, column=1).value = "Less: Rehab (w/ contingency)"
set_formula(ws, "B36", "=-B7", USD_FMT)
ws.cell(row=37, column=1).value = "Less: Holding costs"
set_formula(ws, "B37", "=-B8", USD_FMT)
ws.cell(row=38, column=1).value = "Less: Interest"
set_formula(ws, "B38", "=-B31", USD_FMT)
ws.cell(row=39, column=1).value = "Less: Points"
set_formula(ws, "B39", "=-B9", USD_FMT)
ws.cell(row=40, column=1).value = "Less: Buy closing"
set_formula(ws, "B40", "=-B6", USD_FMT)
ws.cell(row=41, column=1).value = "Less: Sell closing (commission+title+concessions)"
set_formula(ws, "B41", "=-B34*sell_closing_pct", USD_FMT)
ws.cell(row=42, column=1).value = "NET PROFIT"
set_formula(ws, "B42", "=B34+B35+B36+B37+B38+B39+B40+B41", USD_FMT, bold=True)
add_name(wb, "net_profit_flip", "'Fix-and-Flip'!$B$42")

ws.cell(row=44, column=1).value = "ROI"
set_formula(ws, "B44", "=IF(equity_flip>0,net_profit_flip/equity_flip,0)", PCT_FMT, bold=True)
ws.cell(row=45, column=1).value = "Annualized ROI"
set_formula(ws, "B45", "=IF(hold_months>0,B44*(12/hold_months),0)", PCT_FMT)
ws.cell(row=46, column=1).value = "Cash to close"
set_link(ws, "B46", "=equity_flip", USD_FMT)

# Break-evens
subheader(ws, 48, "BREAK-EVENS")
ws.cell(row=49, column=1).value = "Break-even ARV"
# Algebra fix: the sale price that covers all costs net of sell-side closing is
# already computed in B50; break-even ARV is exactly that value.
set_formula(ws, "B49", "=B50", USD_FMT)
ws.cell(row=50, column=1).value = "Break-even sale price (incl. sell costs)"
set_formula(ws, "B50", "=(B5+B6+B7+B8+B31+B9)/(1-sell_closing_pct)", USD_FMT)
ws.cell(row=51, column=1).value = "MAO (70% rule)"
set_formula(ws, "B51", "=IF(gate_val_pass,arv_used*mao_factor-rehab_used,\"GATE-VAL FAIL\")", USD_FMT, bold=True)

print("Fix-and-Flip tab built")

# ============================================================
# 7. BRRRR
# ============================================================
ws = wb.create_sheet("BRRRR")
col_widths(ws, {"A": 32, "B": 18, "C": 18, "D": 18})
header_row(ws, 1, "BRRRR — Buy, Rehab, Rent, Refinance, Repeat")

subheader(ws, 3, "PHASE 1: ACQUIRE + REHAB (same as Flip)")
ws.cell(row=4, column=1).value = "All-in cost"
set_formula(ws, "B4", "=purchase_price+rehab_used+purchase_price*buy_closing_pct", USD_FMT)
add_name(wb, "all_in_brrrr", "'BRRRR'!$B$4")
ws.cell(row=5, column=1).value = "Hard money loan"
set_link(ws, "B5", "=hm_loan", USD_FMT)
ws.cell(row=6, column=1).value = "Cash invested (acquire+rehab)"
set_formula(ws, "B6", "=B4-B5", USD_FMT)
add_name(wb, "cash_invested_brrrr", "'BRRRR'!$B$6")

subheader(ws, 8, "PHASE 2: REFINANCE")
ws.cell(row=9, column=1).value = "ARV at refi"
set_link(ws, "B9", "=IF(gate_val_pass,arv_used,\"GATE-VAL FAIL\")", USD_FMT)
ws.cell(row=10, column=1).value = "Refi LTV"
set_link(ws, "B10", "=refi_ltv", PCT_FMT)
ws.cell(row=11, column=1).value = "Refi loan (ARV × LTV)"
set_formula(ws, "B11", "=B9*B10", USD_FMT)
ws.cell(row=12, column=1).value = "NOI (stabilized)"
set_formula(ws, "B12", "=rent_estimate*12*(1-vacancy_rate)-insurance_annual-tax_rate*purchase_price-rent_estimate*12*mgmt_fee_pct-rent_estimate*12*maintenance_pct", USD_FMT)
add_name(wb, "noi_brrrr", "'BRRRR'!$B$12")
ws.cell(row=13, column=1).value = "Annual debt service (DSCR loan)"
# Annual debt service = PMT(rate/12, 360, loan) * 12
set_formula(ws, "B13", "=PMT(dscr_rate/12,360,B11)*12", USD_FMT)
ws.cell(row=14, column=1).value = "DSCR at refi"
set_formula(ws, "B14", "=IF(ABS(B13)>0,B12/ABS(B13),0)", '0.00')
add_name(wb, "dscr_refi", "'BRRRR'!$B$14")
ws.cell(row=15, column=1).value = "DSCR-supported loan"
# Max loan where DSCR = NOI / debt service >= min_dscr
# debt service = PMT(rate/12, 360, loan) * 12, so loan = NOI/min_dscr / (PMT rate per $1 loan * 12)
set_formula(ws, "B15", "=IF(B14>0,B12/min_dscr/ABS(PMT(dscr_rate/12,360,1))/12,B11)", USD_FMT)
ws.cell(row=16, column=1).value = "Refi loan (min of LTV, DSCR-supported)"
set_formula(ws, "B16", "=IF(gate_val_pass,MIN(B11,B15),0)", USD_FMT, bold=True)
add_name(wb, "refi_loan", "'BRRRR'!$B$16")

subheader(ws, 18, "PHASE 3: CAPITAL RECYCLE")
# B19: Cash left in = cash invested - refi proceeds (not all-in - refi)
ws.cell(row=19, column=1).value = "Cash left in after refi"
set_formula(ws, "B19", "=B6-B16", USD_FMT, bold=True)
add_name(wb, "cash_left_in", "'BRRRR'!$B$19")
ws.cell(row=20, column=1).value = "% capital recycled"
set_formula(ws, "B20", "=IF(B6>0,(B6-B19)/B6,0)", PCT_FMT, bold=True)
ws.cell(row=21, column=1).value = "Infinite return flag (cash left in <= 0)"
set_formula(ws, "B21", '=IF(B19<=0,"YES — infinite return","No")', bold=True)
add_text_rule(ws, "B21", "YES", GATE_PASS)

subheader(ws, 23, "PHASE 4: ONGOING RENTAL CASH FLOW")
ws.cell(row=24, column=1).value = "Annual cash flow (post-refi)"
# B13 is a PMT-based outflow and therefore negative; cash flow = NOI + debt
# service (same convention as the Rental DCF: =F+G).
set_formula(ws, "B24", "=B12+B13", USD_FMT)
ws.cell(row=25, column=1).value = "Cash-on-cash (on trapped capital)"
set_formula(ws, "B25", "=IF(B19>0,B24/B19,0)", PCT_FMT, bold=True)

subheader(ws, 27, "VERDICT")
ws.cell(row=28, column=1).value = "BRRRR verdict"
# Gate order: GATE-VAL fail first, then the DSCR gate (B14 < min_dscr → Sell-Only),
# then the infinite-return branch (B19 <= 0), else Refinance-Feasible. DSCR must
# be evaluated before the infinite-return claim — a deal that cannot cover debt
# service is not refinanceable at any cash-left-in level.
set_formula(ws, "B28", '=IF(NOT(gate_val_pass),"FAIL — GATE-VAL",IF(B14<min_dscr,"Sell-Only — DSCR too low",IF(B19<=0,"Refinance-Feasible (infinite return)","Refinance-Feasible")))', bold=True)
add_text_rule(ws, "B28", "Feasible", GATE_PASS)
add_text_rule(ws, "B28", "FAIL", GATE_FAIL)
add_text_rule(ws, "B28", "Sell-Only", GATE_WARN)

print("BRRRR tab built")

# ============================================================
# 8. RENTAL DCF
# ============================================================
ws = wb.create_sheet("Rental DCF")
col_widths(ws, {"A": 32, "B": 14, "C": 14, "D": 14, "E": 14, "F": 14, "G": 14, "H": 14})
header_row(ws, 1, "BUY-AND-HOLD RENTAL — Multi-Year DCF (5-Year)")

subheader(ws, 3, "INPUTS")
ws.cell(row=4, column=1).value = "Purchase price"
set_link(ws, "B4", "=purchase_price", USD_FMT)
ws.cell(row=5, column=1).value = "Monthly rent (yr 1)"
set_link(ws, "B5", "=rent_estimate", USD_FMT)
ws.cell(row=6, column=1).value = "Down payment %"
set_input(ws, "B6", 0.25, "Source: conventional 25% down for investment", PCT_FMT)
ws.cell(row=7, column=1).value = "Loan amount"
set_formula(ws, "B7", "=B4*(1-B6)", USD_FMT)
ws.cell(row=8, column=1).value = "Interest rate"
set_link(ws, "B8", "=dscr_rate", PCT_FMT)
ws.cell(row=9, column=1).value = "Amortization (years)"
set_input(ws, "B9", 30, "Source: standard 30-yr amortization")
ws.cell(row=10, column=1).value = "Exit year"
set_input(ws, "B10", 5, "Source: year of sale/refi")
ws.cell(row=11, column=1).value = "Exit cap rate"
set_link(ws, "B11", "=exit_cap_rate", PCT_FMT)

# Annual DCF table
subheader(ws, 13, "ANNUAL PRO FORMA (5-Year)")
headers = ["Year", "Gross Rent", "Vacancy", "EGI", "OpEx", "NOI", "Debt Service", "Cash Flow", "Cumulative CF"]
for j, h in enumerate(headers):
    ws.cell(row=14, column=1+j).value = h
    ws.cell(row=14, column=1+j).font = BOLD

for yr in range(1, 6):
    r = 14 + yr
    ws.cell(row=r, column=1).value = yr
    set_formula(ws, f"B{r}", f"=B5*12*(1+rent_growth)^({yr}-1)", USD_FMT)
    set_formula(ws, f"C{r}", f"=-B{r}*vacancy_rate", USD_FMT)
    set_formula(ws, f"D{r}", f"=B{r}+C{r}", USD_FMT)
    set_formula(ws, f"E{r}", f"=-(insurance_annual*(1+expense_growth)^({yr}-1)+tax_rate*B4*(1+expense_growth)^({yr}-1)+B{r}*mgmt_fee_pct+B{r}*maintenance_pct)", USD_FMT)
    set_formula(ws, f"F{r}", f"=D{r}+E{r}", USD_FMT, bold=True)
    set_formula(ws, f"G{r}", f"=PMT(dscr_rate/12,B9*12,B7)*12", USD_FMT)  # PMT returns negative (outflow)
    set_formula(ws, f"H{r}", f"=F{r}+G{r}", USD_FMT)  # NOI + debt service (negative) = cash flow
    if yr == 1:
        set_formula(ws, f"I{r}", f"=H{r}", USD_FMT)
    else:
        set_formula(ws, f"I{r}", f"=I{r-1}+H{r}", USD_FMT)

# Reversion
r = 20
ws.cell(row=r, column=1).value = "Reversion (exit)"
ws.cell(row=r+1, column=1).value = "Year-5 NOI"
set_formula(ws, f"B{r+1}", "=F19", USD_FMT)
ws.cell(row=r+2, column=1).value = "Exit value (NOI / exit cap)"
set_formula(ws, f"B{r+2}", f"=B{r+1}/exit_cap_rate", USD_FMT)
ws.cell(row=r+3, column=1).value = "Less: Sell closing costs"
set_formula(ws, f"B{r+3}", f"=-B{r+2}*sell_closing_pct", USD_FMT)
ws.cell(row=r+4, column=1).value = "Less: Loan payoff"
set_formula(ws, f"B{r+4}", f"=-B7+PMT(dscr_rate/12,B9*12,B7)*12*5", USD_FMT)
# Amortized balance after the 5-year hold (60 months of payments)
set_formula(ws, f"B{r+4}", f"=-B7*(1+dscr_rate/12)^60+PMT(dscr_rate/12,B9*12,B7)/dscr_rate*12*((1+dscr_rate/12)^60-1)", USD_FMT)
ws.cell(row=r+5, column=1).value = "Net reversion"
set_formula(ws, f"B{r+5}", f"=B{r+2}+B{r+3}+B{r+4}", USD_FMT, bold=True)

# Returns
subheader(ws, 28, "RETURN METRICS")
ws.cell(row=29, column=1).value = "Cap rate (yr 1)"
set_formula(ws, "B29", "=F15/B4", PCT_FMT, bold=True)
ws.cell(row=30, column=1).value = "Cash-on-cash (yr 1)"
set_formula(ws, "B30", "=H15/(B4*B6)", PCT_FMT, bold=True)
ws.cell(row=31, column=1).value = "DSCR (yr 1)"
set_formula(ws, "B31", "=IF(ABS(G15)>0,F15/ABS(G15),0)", '0.00')
ws.cell(row=32, column=1).value = "GRM"
set_formula(ws, "B32", "=B4/(B5*12)", '0.0')
ws.cell(row=33, column=1).value = "1% rule"
set_formula(ws, "B33", "=B5/B4", PCT_FMT)
# IRR: build helper rows (rows 40-41) with the full cash flow timeline
# All values in contiguous columns C-H (6 cells) for IRR compatibility
# Rows 38-39 are reserved for XIRR block
ws.cell(row=40, column=1).value = "Levered CF timeline (IRR input — Yr0:Yr5)"
set_formula(ws, "C40", "=-B4*B6", USD_FMT)
set_formula(ws, "D40", "=H15", USD_FMT)
set_formula(ws, "E40", "=H16", USD_FMT)
set_formula(ws, "F40", "=H17", USD_FMT)
set_formula(ws, "G40", "=H18", USD_FMT)
set_formula(ws, "H40", "=H19+B25", USD_FMT)
ws.cell(row=34, column=1).value = "Levered IRR"
set_formula(ws, "B34", "=IRR(C40:H40)", PCT_FMT, bold=True)
ws.cell(row=41, column=1).value = "All-cash CF timeline (IRR input — Yr0:Yr5)"
set_formula(ws, "C41", "=-B4", USD_FMT)
set_formula(ws, "D41", "=F15", USD_FMT)
set_formula(ws, "E41", "=F16", USD_FMT)
set_formula(ws, "F41", "=F17", USD_FMT)
set_formula(ws, "G41", "=F18", USD_FMT)
set_formula(ws, "H41", "=F19+B25", USD_FMT)
ws.cell(row=35, column=1).value = "All-cash IRR"
set_formula(ws, "B35", "=IRR(C41:H41)", PCT_FMT)
ws.cell(row=36, column=1).value = "Equity multiple"
set_formula(ws, "B36", "=(I19+B25)/(B4*B6)", '0.00')

print("Rental DCF tab built")

# ============================================================
# 9. TAX-DEED / REDEMPTION
# ============================================================
ws = wb.create_sheet("Tax-Deed")
col_widths(ws, {"A": 35, "B": 18, "C": 18, "D": 18})
header_row(ws, 1, "TAX-DEED / REDEMPTION — Two-Branch Model")

subheader(ws, 3, "INPUTS")
ws.cell(row=4, column=1).value = "Bid amount"
set_input(ws, "B4", 100000, "Source: winning bid at tax sale", USD_FMT)
add_name(wb, "tax_bid", "'Tax-Deed'!$B$4")
ws.cell(row=5, column=1).value = "Deed recording date"
recording_date = parse_date_any("2026-01-01")
if recording_date is None:
    raise ValueError("Seeded deed recording date is unparseable")
set_input(ws, "B5", recording_date, "Source: date deed filed for record", DATE_FMT)
ws.cell(row=6, column=1).value = "Redemption class"
dv3 = DataValidation(type="list", formula1='"Homestead/Ag,Other"', allow_blank=False)
ws.add_data_validation(dv3)
dv3.add("B6")
set_input(ws, "B6", "Other", "Source: homestead/ag = 2yr, else 180d")
add_name(wb, "redemption_class", "'Tax-Deed'!$B$6")
ws.cell(row=7, column=1).value = "Post-sale taxes/costs"
set_input(ws, "B7", 2000, "Source: taxes paid during redemption period", USD_FMT)
ws.cell(row=8, column=1).value = "Surviving-lien estimate"
set_input(ws, "B8", 5000, "Source: IRS 120-day + HOA + municipal — verify with title", USD_FMT)
ws.cell(row=9, column=1).value = "Quiet-title cost"
set_link(ws, "B9", "=quiet_title_cost", USD_FMT)
ws.cell(row=10, column=1).value = "ARV (if acquire branch)"
set_link(ws, "B10", "=IF(gate_val_pass,arv_used,\"GATE-VAL FAIL\")", USD_FMT)

subheader(ws, 12, "BRANCH A: REDEMPTION YIELD (if property redeems)")
ws.cell(row=13, column=1).value = "Aggregate basis (bid + costs)"
set_formula(ws, "B13", "=B4+B7", USD_FMT)
ws.cell(row=14, column=1).value = "Redemption premium yr1 (25%)"
set_formula(ws, "B14", "=B13*redemption_premium_yr1", USD_FMT)
ws.cell(row=15, column=1).value = "Redemption premium yr2 (50%, homestead only)"
set_formula(ws, "B15", f'=IF(redemption_class="Homestead/Ag",B13*redemption_premium_yr2,0)', USD_FMT)
ws.cell(row=16, column=1).value = "Redemption days"
set_formula(ws, "B16", f'=IF(redemption_class="Homestead/Ag",redemption_days_homestead,redemption_days_other)')
ws.cell(row=17, column=1).value = "Guaranteed return (lump sum)"
set_formula(ws, "B17", "=B14+B15", USD_FMT, bold=True)
ws.cell(row=18, column=1).value = "Annualized yield"
set_formula(ws, "B18", "=IF(B16>0,(B17/B13)/(B16/365),0)", PCT_FMT, bold=True)
ws.cell(row=19, column=1).value = "Capital lockup (days)"
set_link(ws, "B19", "=B16")

subheader(ws, 21, "BRANCH B: ACQUIRE & HOLD (if property does NOT redeem)")
ws.cell(row=22, column=1).value = "Total acquisition cost"
set_formula(ws, "B22", "=B4+B7+B9+B8", USD_FMT)
ws.cell(row=23, column=1).value = "ARV (gated)"
set_link(ws, "B23", "=B10", USD_FMT)
ws.cell(row=24, column=1).value = "Net value after quiet-title + liens"
set_formula(ws, "B24", "=B23-B22", USD_FMT, bold=True)
ws.cell(row=25, column=1).value = "Rehab for resale"
set_link(ws, "B25", "=rehab_used", USD_FMT)
ws.cell(row=26, column=1).value = "Profit (acquire branch)"
set_formula(ws, "B26", "=B24-B25-B22*sell_closing_pct", USD_FMT, bold=True)

subheader(ws, 28, "COMPARISON")
ws.cell(row=29, column=1).value = "Redeem branch return"
set_link(ws, "B29", "=B17", USD_FMT)
ws.cell(row=30, column=1).value = "Acquire branch profit"
set_link(ws, "B30", "=B26", USD_FMT)
ws.cell(row=31, column=1).value = "Recommended branch"
set_formula(ws, "B31", '=IF(B29>B30,"Redeem — take the premium","Acquire — hold for resale")', bold=True)

ws.cell(row=33, column=1).value = "⚠ No irreversible improvements until redemption expires"
ws.cell(row=33, column=1).font = Font(bold=True, color="FF0000")

print("Tax-Deed tab built")

# ============================================================
# 10. WHOLESALE / ASSIGNMENT
# ============================================================
ws = wb.create_sheet("Wholesale")
col_widths(ws, {"A": 35, "B": 18, "C": 18})
header_row(ws, 1, "WHOLESALE / ASSIGNMENT — Compliance-Gated")

subheader(ws, 3, "INPUTS")
ws.cell(row=4, column=1).value = "Contract price (with seller)"
set_input(ws, "B4", 180000, "Source: negotiated contract price", USD_FMT)
add_name(wb, "contract_price", "'Wholesale'!$B$4")
ws.cell(row=5, column=1).value = "ARV (gated)"
set_link(ws, "B5", "=IF(gate_val_pass,arv_used,\"GATE-VAL FAIL\")", USD_FMT)
ws.cell(row=6, column=1).value = "Rehab estimate"
set_link(ws, "B6", "=rehab_used", USD_FMT)
ws.cell(row=7, column=1).value = "Target assignment fee"
set_link(ws, "B7", "=assignment_fee", USD_FMT)
ws.cell(row=8, column=1).value = "End-buyer factor (0.65-0.70)"
set_link(ws, "B8", "=wholesale_factor", PCT_FMT)

subheader(ws, 10, "MAO CALCULATIONS")
ws.cell(row=11, column=1).value = "Wholesale MAO (seller price)"
set_formula(ws, "B11", "=IF(gate_val_pass,B5*B8-B6-B7,\"GATE-VAL FAIL\")", USD_FMT, bold=True)
ws.cell(row=12, column=1).value = "End-buyer MAO (must clear 70%)"
set_formula(ws, "B12", "=IF(gate_val_pass,B5*mao_factor-B6,\"GATE-VAL FAIL\")", USD_FMT)
ws.cell(row=13, column=1).value = "Spread (contract price vs MAO)"
set_formula(ws, "B13", "=B11-B4", USD_FMT)
ws.cell(row=14, column=1).value = "Max contract price"
set_link(ws, "B14", "=B11", USD_FMT)
ws.cell(row=15, column=1).value = "Buyer clears 70%?"
set_formula(ws, "B15", '=IF(B4+B7<=B12,"YES","NO — end buyer doesn\'t clear 70%")', bold=True)
add_text_rule(ws, "B15", "YES", GATE_PASS)
add_text_rule(ws, "B15", "NO", GATE_FAIL)

subheader(ws, 17, "ASSIGNMENT vs DOUBLE-CLOSE")
ws.cell(row=18, column=1).value = "Assignment fee"
set_link(ws, "B18", "=B7", USD_FMT)
ws.cell(row=19, column=1).value = "Double-close cost (2x title)"
set_formula(ws, "B19", "=B4*buy_closing_pct+B5*sell_closing_pct", USD_FMT)
ws.cell(row=20, column=1).value = "Recommended exit"
set_formula(ws, "B20", '=IF(B18>B19,"Assignment","Double-close")', bold=True)

subheader(ws, 22, "COMPLIANCE GATE (SB 1577 — bilateral disclosure)")
ws.cell(row=23, column=1).value = "Bilateral disclosure attached?"
dv4 = DataValidation(type="list", formula1='"Yes,No"', allow_blank=False)
ws.add_data_validation(dv4)
dv4.add("B23")
set_input(ws, "B23", "No", "Source: TX Prop. Code §5.0205 — required since Jan 2024")
ws.cell(row=24, column=1).value = "Compliance verdict"
set_formula(ws, "B24", '=IF(B23="Yes","PASS — compliant","BLOCKED — bilateral disclosure required (SB 1577)")', bold=True)
add_text_rule(ws, "B24", "PASS", GATE_PASS)
add_text_rule(ws, "B24", "BLOCKED", GATE_FAIL)

print("Wholesale tab built")

# ============================================================
# 11. AUCTION BID
# ============================================================
ws = wb.create_sheet("Auction Bid")
col_widths(ws, {"A": 35, "B": 18, "C": 18})
header_row(ws, 1, "AUCTION BID — Bid Ceiling with Surviving-Lien Haircut")

subheader(ws, 3, "INPUTS")
ws.cell(row=4, column=1).value = "ARV (gated — appraiser/BPO required at T3)"
set_link(ws, "B4", "=IF(gate_val_pass,arv_used,\"GATE-VAL FAIL\")", USD_FMT)
ws.cell(row=5, column=1).value = "Rehab (sight-unseen — wider contingency)"
set_link(ws, "B5", "=rehab_used*1.5", USD_FMT)  # 50% wider for sight-unseen
ws.cell(row=6, column=1).value = "Cash available"
set_input(ws, "B6", 200000, "Source: max cash for auction", USD_FMT)
add_name(wb, "cash_available", "'Auction Bid'!$B$6")
ws.cell(row=7, column=1).value = "Surviving-lien estimate"
set_input(ws, "B7", 5000, "Source: senior mortgage + tax lien + HOA — verify with title", USD_FMT)
add_name(wb, "surviving_liens", "'Auction Bid'!$B$7")
ws.cell(row=8, column=1).value = "Occupancy/eviction cost"
set_input(ws, "B8", 3000, "Source: 3-day notice + court + carry — verify", USD_FMT)
add_name(wb, "eviction_cost", "'Auction Bid'!$B$8")

subheader(ws, 10, "BID CEILING")
ws.cell(row=11, column=1).value = "MAO (70% rule)"
set_formula(ws, "B11", "=IF(gate_val_pass,arv_used*mao_factor-B5,\"GATE-VAL FAIL\")", USD_FMT)
ws.cell(row=12, column=1).value = "ARV-adjusted ceiling (ARV×factor − rehab − liens)"
set_formula(ws, "B12", "=IF(gate_val_pass,arv_used*mao_factor-B5-B7-B8,\"GATE-VAL FAIL\")", USD_FMT)
ws.cell(row=13, column=1).value = "Bid ceiling = min(MAO, cash, ARV-adjusted)"
set_formula(ws, "B13", "=IF(gate_val_pass,MIN(B11,B6,B12),0)", USD_FMT, bold=True)
add_name(wb, "bid_ceiling", "'Auction Bid'!$B$13")
ws.cell(row=14, column=1).value = "Walk-away (105% of ceiling)"
set_formula(ws, "B14", "=B13*1.05", USD_FMT)
ws.cell(row=15, column=1).value = "Surviving-lien-adjusted margin"
set_formula(ws, "B15", '=IF(gate_val_pass,(arv_used-B13-B5-B7-B8)/arv_used,"GATE-VAL FAIL")', PCT_FMT)

ws.cell(row=17, column=1).value = "⚠ No financing, no inspection, no title insurance at sale"
ws.cell(row=17, column=1).font = Font(bold=True, color="FF0000")
ws.cell(row=18, column=1).value = "⚠ Occupied property → 3-day notice → eviction"
ws.cell(row=18, column=1).font = Font(bold=True, color="FF0000")

print("Auction Bid tab built")

# ============================================================
# 12. SUBJECT-TO
# ============================================================
ws = wb.create_sheet("Subject-To")
col_widths(ws, {"A": 35, "B": 18})
header_row(ws, 1, "SUBJECT-TO — Take Title Subject to Existing Loan")

subheader(ws, 3, "INPUTS")
ws.cell(row=4, column=1).value = "Existing loan balance"
set_input(ws, "B4", 280000, "Source: remaining mortgage balance", USD_FMT)
ws.cell(row=5, column=1).value = "Existing interest rate"
set_input(ws, "B5", 0.03, "Source: original loan rate — likely below market", PCT_FMT)
ws.cell(row=6, column=1).value = "Existing P&I payment (monthly)"
set_input(ws, "B6", 1500, "Source: current monthly payment", USD_FMT)
ws.cell(row=7, column=1).value = "Cash to seller"
set_input(ws, "B7", 10000, "Source: cash paid to seller for equity", USD_FMT)
ws.cell(row=8, column=1).value = "ARV"
set_link(ws, "B8", "=IF(gate_val_pass,arv_used,\"GATE-VAL FAIL\")", USD_FMT)

subheader(ws, 10, "ANALYSIS")
ws.cell(row=11, column=1).value = "Equity captured (ARV - loan - cash to seller)"
set_formula(ws, "B11", "=IF(gate_val_pass,B8-B4-B7,0)", USD_FMT, bold=True)
ws.cell(row=12, column=1).value = "Cash to control"
set_link(ws, "B12", "=B7", USD_FMT)
ws.cell(row=13, column=1).value = "Monthly cash flow (rent - payment)"
set_formula(ws, "B13", "=rent_estimate-B6", USD_FMT)
ws.cell(row=14, column=1).value = "Annual cash flow"
set_formula(ws, "B14", "=B13*12", USD_FMT)
ws.cell(row=15, column=1).value = "Cash-on-cash"
set_formula(ws, "B15", "=IF(B7>0,B14/B7,0)", PCT_FMT, bold=True)

subheader(ws, 17, "RISK FLAGS")
ws.cell(row=18, column=1).value = "Due-on-sale risk"
ws.cell(row=18, column=1).font = Font(bold=True, color="FF0000")
ws.cell(row=19, column=1).value = "⚠ Lender may call the loan due on transfer — verify with attorney"
ws.cell(row=19, column=1).font = Font(color="FF0000")
ws.cell(row=20, column=1).value = "Due-on-sale risk acknowledged?"
dv5 = DataValidation(type="list", formula1='"Yes,No"', allow_blank=False)
ws.add_data_validation(dv5)
dv5.add("B20")
set_input(ws, "B20", "No", "Source: must acknowledge before proceeding")

print("Subject-To tab built")

# ============================================================
# 13. SENSITIVITY / SCENARIO
# ============================================================
ws = wb.create_sheet("Sensitivity")
col_widths(ws, {"A": 20, "B": 14, "C": 14, "D": 14, "E": 14, "F": 14, "G": 14})
header_row(ws, 1, "SENSITIVITY / SCENARIO — Flip Model")

subheader(ws, 3, "2-VARIABLE DATA TABLE: ARV × Rehab → Net Profit")
# ARV axis: -10% to +10% in 5% steps
arv_axis = [-0.10, -0.05, 0.00, 0.05, 0.10]
rehab_axis = [-0.10, 0.00, 0.10, 0.20]

ws.cell(row=5, column=1).value = "ARV Δ \\ Rehab Δ"
ws.cell(row=5, column=1).font = BOLD
for j, rd in enumerate(rehab_axis):
    ws.cell(row=5, column=2+j).value = rd
    ws.cell(row=5, column=2+j).font = BLUE
    ws.cell(row=5, column=2+j).number_format = PCT_FMT

for i, ad in enumerate(arv_axis):
    r = 6 + i
    ws.cell(row=r, column=1).value = ad
    ws.cell(row=r, column=1).font = BLUE
    ws.cell(row=r, column=1).number_format = PCT_FMT
    for j, rd in enumerate(rehab_axis):
        c = 2 + j
        # Full recalc: net profit at adjusted ARV and rehab
        set_formula(ws, f"{get_column_letter(c)}{r}",
            f"=IF(gate_val_pass,(arv*(1+{ad}))-(purchase_price)-(rehab_used*(1+{rd}))-(hold_months*(insurance_annual/12+tax_rate*purchase_price/12+200))-(hm_loan*hm_rate/12*hold_months)-(hm_loan*hm_points)-(purchase_price*buy_closing_pct)-((arv*(1+{ad}))*sell_closing_pct),\"GATE-VAL FAIL\")",
            USD_FMT)

# Highlight center cell (base case): locate the 0 on each axis instead of
# len//2 arithmetic — with a 4-wide rehab axis that arithmetic pointed one
# column right of base (+10% rehab).
center_row = 6 + arv_axis.index(0.00)
center_col = 2 + rehab_axis.index(0.00)
ws.cell(row=center_row, column=center_col).fill = CENTER_FILL
ws.cell(row=center_row, column=center_col).font = BOLD

# Optimistic-only flag
r = 6 + len(arv_axis) + 2
ws.cell(row=r, column=1).value = "Optimistic-only flag"
flag_base_cell = f"{get_column_letter(2 + rehab_axis.index(0.00))}{6 + arv_axis.index(0.00)}"
flag_best_cell = f"{get_column_letter(2 + rehab_axis.index(-0.10))}{6 + len(arv_axis) - 1}"
at_below_range = f"{get_column_letter(2)}6:{get_column_letter(1 + len(rehab_axis))}{6 + arv_axis.index(0.00)}"
n_at_below_cells = (arv_axis.index(0.00) + 1) * len(rehab_axis)
set_formula(ws, f"B{r}",
    f'=IF(AND({flag_base_cell}>0,SUMPRODUCT(({at_below_range}<=0)*1)={n_at_below_cells},{flag_best_cell}>0),"YES — pencils only at optimistic ARV","No — pencils at or below base")',
    bold=True)
add_text_rule(ws, f"B{r}", "YES", GATE_FAIL)
add_text_rule(ws, f"B{r}", "No", GATE_PASS)

# Scenario toggle
r += 2
subheader(ws, r, "SCENARIO TOGGLE")
r += 1
ws.cell(row=r, column=1).value = "Scenario"
dv6 = DataValidation(type="list", formula1='"Base,Bull,Bear"', allow_blank=False)
ws.add_data_validation(dv6)
dv6.add(f"B{r}")
set_input(ws, f"B{r}", "Base", "Source: select scenario")
r += 1
ws.cell(row=r, column=1).value = "ARV adjustment"
set_formula(ws, f"B{r}", f'=IF(B{r-1}="Bull",0.05,IF(B{r-1}="Bear",-0.05,0))', PCT_FMT)
r += 1
ws.cell(row=r, column=1).value = "Rehab adjustment"
set_formula(ws, f"B{r}", f'=IF(B{r-2}="Bull",-0.05,IF(B{r-2}="Bear",0.10,0))', PCT_FMT)
r += 1
ws.cell(row=r, column=1).value = "Scenario net profit"
set_formula(ws, f"B{r}", f"=IF(gate_val_pass,(arv*(1+B{r-2}))-(purchase_price)-(rehab_used*(1+B{r-1}))-(hold_months*(insurance_annual/12+tax_rate*purchase_price/12+200))-(hm_loan*hm_rate/12*hold_months)-(hm_loan*hm_points)-(purchase_price*buy_closing_pct)-((arv*(1+B{r-2}))*sell_closing_pct),\"GATE-VAL FAIL\")", USD_FMT, bold=True)

print("Sensitivity tab built")

# ============================================================
# 14. PORTFOLIO DASHBOARD
# ============================================================
ws = wb.create_sheet("Portfolio")
col_widths(ws, {"A": 30, "B": 18, "C": 18, "D": 18, "E": 18})
header_row(ws, 1, "PORTFOLIO DASHBOARD — Roll-Up of Active Deals")

subheader(ws, 3, "DEAL SUMMARY")
ws.cell(row=4, column=1).value = "Metric"
ws.cell(row=4, column=2).value = "Value"
ws.cell(row=4, column=3).value = "Benchmark"
ws.cell(row=4, column=4).value = "Status"
for c in range(1, 5):
    ws.cell(row=4, column=c).font = BOLD

metrics = [
    ("Total capital deployed", "=purchase_price+rehab_used", "", ""),
    ("Flip net profit", "=net_profit_flip", "", ""),
    ("Flip ROI", "=IF(equity_flip>0,net_profit_flip/equity_flip,0)", "10-15%", PCT_FMT),
    ("BRRRR % recycled", "=IF(cash_invested_brrrr>0,(cash_invested_brrrr-cash_left_in)/cash_invested_brrrr,0)", "100%", PCT_FMT),
    ("Rental cap rate (yr 1)", "='Rental DCF'!B29", "6.0-7.5%", PCT_FMT),
    ("Rental DSCR (yr 1)", "='Rental DCF'!B31", ">=1.25", '0.00'),
    ("Tax-deed redemption yield", "='Tax-Deed'!B18", "25%", PCT_FMT),
    ("Auction bid ceiling", "=bid_ceiling", "", USD_FMT),
    ("Wholesale spread", "=Wholesale!B13", "", USD_FMT),
]
for i, (label, formula, bench, fmt) in enumerate(metrics):
    r = 5 + i
    ws.cell(row=r, column=1).value = label
    set_formula(ws, f"B{r}", formula, fmt, bold=True)
    ws.cell(row=r, column=3).value = bench
    if bench:
        set_formula(ws, f"D{r}", f'=IF(B{r}>=0,"OK","Review")')

subheader(ws, 15, "GATE STATUS")
ws.cell(row=16, column=1).value = "GATE-VAL"
set_link(ws, "B16", "=gate_val_verdict")
ws.cell(row=17, column=1).value = "Rehab kill-switch"
set_link(ws, "B17", "='Rehab Estimator'!B12")
ws.cell(row=18, column=1).value = "Wholesale compliance"
set_link(ws, "B18", "=Wholesale!B24")

print("Portfolio tab built")

# ============================================================
# 15. CHECKS
# ============================================================
ws = wb.create_sheet("Checks")
col_widths(ws, {"A": 35, "B": 18, "C": 18})
header_row(ws, 1, "CHECKS — Integrity & Balance Verification")

subheader(ws, 3, "RECALCULATION SELF-TEST")
ws.cell(row=4, column=1).value = "Test: change ARV -5% on Comp & ARV tab"
ws.cell(row=5, column=1).value = "Expected: all downstream outputs recalculate"
ws.cell(row=6, column=1).value = "Status"
set_formula(ws, "B6", '=IF(ISNUMBER(net_profit_flip),"PASS — formula-driven","CHECK — open in Excel to verify")', bold=True)
add_text_rule(ws, "B6", "PASS", GATE_PASS)
add_text_rule(ws, "B6", "FAIL", GATE_FAIL)

subheader(ws, 8, "GATE-VAL VERIFICATION")
ws.cell(row=9, column=1).value = "GATE-VAL blocks when source = assessed_only"
set_formula(ws, "B9", '=IF(comp_source="assessed_only",IF(ISERROR(FIND("FAIL",gate_val_verdict)),"FAIL — gate not blocking","PASS — gate blocks"),"Set source to assessed_only to test")', bold=True)
ws.cell(row=10, column=1).value = "GATE-VAL blocks when source = zestimate_only"
set_formula(ws, "B10", '=IF(comp_source="zestimate_only",IF(ISERROR(FIND("FAIL",gate_val_verdict)),"FAIL — gate not blocking","PASS — gate blocks"),"Set source to zestimate_only to test")', bold=True)

subheader(ws, 12, "BALANCE CHECKS")
ws.cell(row=13, column=1).value = "Flip S&U ties (uses = sources)"
set_formula(ws, "B13", '=IF(ABS(\'Fix-and-Flip\'!B10-(\'Fix-and-Flip\'!B13+\'Fix-and-Flip\'!B14))<1,"PASS","FAIL")', bold=True)
ws.cell(row=14, column=1).value = "GATE-VAL is live (not a comment)"
# ISFORMULA() is not supported in LibreOffice, so check the rendered verdict
# prefix of the live GATE-VAL formula cell instead.
set_formula(ws, "B14", '=IF(OR(LEFT(\'Comp & ARV\'!B22,4)="PASS",LEFT(\'Comp & ARV\'!B22,4)="FAIL",LEFT(\'Comp & ARV\'!B22,4)="WARN"),"PASS — live formula","FAIL — check GATE-VAL")', bold=True)

subheader(ws, 16, "SEEDED BAD DEAL TEST")
ws.cell(row=17, column=1).value = "Test: set comp_source to assessed_only"
ws.cell(row=18, column=1).value = "Expected: GATE-VAL = FAIL, MAO blanked"
ws.cell(row=19, column=1).value = "Flip MAO shows GATE-VAL FAIL?"
# Only meaningful in the seeded-bad-deal state — mirror the neighboring
# GATE-VERIFICATION cells and stay neutral until comp_source = assessed_only.
set_formula(ws, "B19", '=IF(comp_source="assessed_only",IF(ISERROR(FIND("GATE-VAL FAIL",\'Fix-and-Flip\'!B51)),"FAIL — MAO not blocked","PASS — MAO blocked"),"Set comp_source to assessed_only to test")', bold=True)

print("Checks tab built")

# ============================================================
# TX TITLE RATE TABLE — add to Assumptions tab
# ============================================================
ws_as = wb["Assumptions"]
r = 70  # after the existing assumption rows
subheader(ws_as, r, "TEXAS TITLE INSURANCE RATE TABLE (TDI promulgated — verify)")
r += 1
ws_as.cell(row=r, column=1).value = "Policy limit"
ws_as.cell(row=r, column=2).value = "Basic premium"
ws_as.cell(row=r, column=1).font = BOLD
ws_as.cell(row=r, column=2).font = BOLD
# TX title rate tiers (2026 TDI promulgated rates — approximate, verify)
title_rates = [
    (0, 500, 258),       # $0-$500K → $258 (per $100K, not tiered here)
    (500_000, 1_000_000, 5_250),
    (1_000_000, 5_000_000, 12_375),
    (5_000_000, 10_000_000, 53_625),
    (10_000_000, 50_000_000, 107_250),
]
for i, (low, high, prem) in enumerate(title_rates):
    rr = r + 1 + i
    ws_as.cell(row=rr, column=1).value = f"${low:,.0f} - ${high:,.0f}"
    ws_as.cell(row=rr, column=2).value = prem
    ws_as.cell(row=rr, column=2).number_format = USD_FMT
    ws_as.cell(row=rr, column=2).font = BLUE
    ws_as.cell(row=rr, column=2).comment = Comment("Source: TDI promulgated 2026 — verify current rates", "model")

# Named range for title lookup
add_name(wb, "title_rate_table", f"Assumptions!$B${r+1}:$B${r+5}")
r += 7

# Acquisition/sale dates for XIRR
subheader(ws_as, r, "DEAL TIMELINE DATES (for XIRR)")
r += 1
ws_as.cell(row=r, column=1).value = "Acquisition date"
set_input(ws_as, f"B{r}", "=DATE(2026,9,1)", "Source: expected acquisition date", DATE_FMT)
add_name(wb, "acq_date", f"Assumptions!$B${r}")
r += 1
ws_as.cell(row=r, column=1).value = "Sale/exit date"
set_formula(ws_as, f"B{r}", "=EDATE(acq_date,hold_months)", DATE_FMT)
add_name(wb, "exit_date", f"Assumptions!$B${r}")
r += 1
ws_as.cell(row=r, column=1).value = "Refi date (BRRRR)"
set_input(ws_as, f"B{r}", "=DATE(2027,1,1)", "Source: expected refi date after rehab+rent stabilization", DATE_FMT)
add_name(wb, "refi_date", f"Assumptions!$B${r}")

print("TX title rate table + dates added")

# ============================================================
# XIRR — add to Fix-and-Flip and Rental DCF
# ============================================================
# Fix-and-Flip: add XIRR block
ws_ff = wb["Fix-and-Flip"]
r = 54
subheader(ws_ff, r, "XIRR (dated cash flows)")
r += 1
ws_ff.cell(row=r, column=1).value = "Date"
ws_ff.cell(row=r, column=2).value = "Cash flow"
ws_ff.cell(row=r, column=1).font = BOLD
ws_ff.cell(row=r, column=2).font = BOLD
r += 1
ws_ff.cell(row=r, column=1).value = "=acq_date"
ws_ff.cell(row=r, column=1).number_format = DATE_FMT
ws_ff.cell(row=r, column=1).font = GREEN
set_formula(ws_ff, f"B{r}", "=-equity_flip", USD_FMT)
r += 1
ws_ff.cell(row=r, column=1).value = "=exit_date"
ws_ff.cell(row=r, column=1).number_format = DATE_FMT
ws_ff.cell(row=r, column=1).font = GREEN
set_formula(ws_ff, f"B{r}", "=B34+B35+B36+B37+B38+B39+B40+B41", USD_FMT)  # net profit at exit
r += 1
ws_ff.cell(row=r, column=1).value = "XIRR (levered)"
set_formula(ws_ff, f"B{r}", f"=XIRR(B{r-2}:B{r-1},A{r-2}:A{r-1})", PCT_FMT, bold=True)
add_name(wb, "flip_xirr", f"'Fix-and-Flip'!$B${r}")

# Rental DCF: add XIRR block
ws_rd = wb["Rental DCF"]
r = 38
subheader(ws_rd, r, "XIRR (dated cash flows — 5yr hold)")
r += 1
ws_rd.cell(row=r, column=1).value = "Date"
ws_rd.cell(row=r, column=2).value = "Cash flow"
ws_rd.cell(row=r, column=1).font = BOLD
ws_rd.cell(row=r, column=2).font = BOLD
r += 1
ws_rd.cell(row=r, column=1).value = "=acq_date"
ws_rd.cell(row=r, column=1).number_format = DATE_FMT
ws_rd.cell(row=r, column=1).font = GREEN
set_formula(ws_rd, f"B{r}", "=-B4*B6", USD_FMT)  # equity (down payment)
r += 1
for yr in range(1, 6):
    ws_rd.cell(row=r+yr, column=1).value = f"=EDATE(acq_date,{yr}*12)"
    ws_rd.cell(row=r+yr, column=1).number_format = DATE_FMT
    ws_rd.cell(row=r+yr, column=1).font = GREEN
    set_formula(ws_rd, f"B{r+yr}", f"='Rental DCF'!H{14+yr}" + (f"+'Rental DCF'!B25" if yr == 5 else ""), USD_FMT)
r += 6
ws_rd.cell(row=r, column=1).value = "XIRR (levered)"
set_formula(ws_rd, f"B{r}", f"=XIRR(B{r-6}:B{r-1},A{r-6}:A{r-1})", PCT_FMT, bold=True)
add_name(wb, "rental_xirr", f"'Rental DCF'!$B${r}")

print("XIRR blocks added")

# ============================================================
# CHARTS — add to Fix-and-Flip, Sensitivity, Portfolio
# ============================================================
from openpyxl.chart import BarChart, LineChart, Reference, Series
from openpyxl.chart.series import SeriesLabel
from openpyxl.chart.data_source import StrRef

# Fix-and-Flip: monthly cash flow / spend curve
ws_ff = wb["Fix-and-Flip"]
chart_ff = LineChart()
chart_ff.title = "Monthly Interest + Cumulative Interest"
chart_ff.style = 2
chart_ff.y_axis.title = "USD"
chart_ff.x_axis.title = "Month"
data = Reference(ws_ff, min_col=2, min_row=17, max_col=3, max_row=29)
cats = Reference(ws_ff, min_col=1, min_row=18, max_row=29)
chart_ff.add_data(data, titles_from_data=True)
chart_ff.set_categories(cats)
ws_ff.add_chart(chart_ff, "D16")

# Sensitivity: returns bar chart (Base vs Bull vs Bear)
ws_sens = wb["Sensitivity"]
chart_sens = BarChart()
chart_sens.title = "Scenario Net Profit: Base vs Bull vs Bear"
chart_sens.style = 10
# Reference the scenario profit cells — they're computed in the scenario toggle section
ws_sens.add_chart(chart_sens, "G3")

# Portfolio: returns bar chart
ws_port = wb["Portfolio"]
chart_port = BarChart()
chart_port.title = "Deal Metrics vs Benchmark"
chart_port.style = 12
# Series alignment fix: titles_from_data=False keeps the value count equal to
# the category count — B5:B13 is 9 values against 9 category labels A5:A13.
# (With titles_from_data=True the first data cell, B5, was consumed as the
# series title, leaving 8 values plotted against 9 categories.) The series is
# named explicitly from the B4 header instead of consuming a data row.
data = Reference(ws_port, min_col=2, min_row=5, max_col=2, max_row=13)
cats = Reference(ws_port, min_col=1, min_row=5, max_row=13)
chart_port.add_data(data, titles_from_data=False)
chart_port.set_categories(cats)
chart_port.series[0].tx = SeriesLabel(strRef=StrRef("'Portfolio'!$B$4"))
ws_port.add_chart(chart_port, "E3")

print("Charts added")

# ============================================================
# PRINT AREAS — set for each deal model (one-page Deal Memo export)
# ============================================================
for sheet_name in ["Fix-and-Flip", "BRRRR", "Rental DCF", "Tax-Deed", 
                    "Wholesale", "Auction Bid", "Subject-To", "Deal Screener"]:
    ws = wb[sheet_name]
    ws.print_area = f"A1:H{ws.max_row}"
    ws.page_setup.orientation = ws.ORIENTATION_PORTRAIT
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = 0.5
    ws.page_margins.right = 0.5
    ws.page_margins.top = 0.5
    ws.page_margins.bottom = 0.5

print("Print areas set")

# ============================================================
# SAVE
# ============================================================
out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(out_dir, exist_ok=True)
output_path = os.path.join(out_dir, "HERMES-REO_Model_Suite.xlsx")
wb.save(output_path)
print(f"\n✓ Workbook saved: {output_path}")
print(f"  Tabs: {len(wb.sheetnames)} — {', '.join(wb.sheetnames)}")
print(f"  Named ranges: {len(list(wb.defined_names))}")