#!/usr/bin/env python3
# scripts/gen_legal_package.py
"""Generate LEGAL_VERIFICATION_PACKAGE.md — the TX attorney verification packet.

src/legal_rules.py carries verified_by="spec_research": every statutory
constant (redemption periods/premiums, disclosure duties, buyer eligibility)
is research-grade, not counsel-approved. This script introspects those
constants (explicit whitelist — no noise) and renders a sign-off table with
blank Attorney Notes so an attorney can verify, correct, and countersign.

Usage:
    python scripts/gen_legal_package.py [--out PATH]     # default: repo root
    python scripts/gen_legal_package.py --self-check
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src import legal_rules as lr          # noqa: E402
from src.io_utils import write_text        # noqa: E402

DEFAULT_OUT = os.path.join(_REPO_ROOT, "LEGAL_VERIFICATION_PACKAGE.md")

UNVERIFIED_BADGE = "UNVERIFIED"


def _status() -> str:
    """verified_by status cell — every row inherits the module-level status."""
    return f"{lr.VERIFIED_BY} ({UNVERIFIED_BADGE})"


def _pct(value: float) -> str:
    return f"{value!r} ({value * 100:.0f}%)"


def _rows() -> list[tuple[str, str, str]]:
    """(Constant, Current Value, Source Citation) pulled from legal_rules."""
    rows: list[tuple[str, str, str]] = []

    # --- R1: Mortgage foreclosure -------------------------------------
    rows += [
        ("FORECLOSURE_STATUTE", str(lr.FORECLOSURE_STATUTE),
         "R1: mortgage foreclosure is non-judicial trustee sale"),
        ("FORECLOSURE_TYPE", str(lr.FORECLOSURE_TYPE),
         "R1 header comment: Mortgage Foreclosure (non-judicial trustee sale)"),
        ("CURE_NOTICE_DAYS", str(lr.CURE_NOTICE_DAYS),
         "R1 timeline comment: '20-day cure'"),
        ("SALE_NOTICE_DAYS", str(lr.SALE_NOTICE_DAYS),
         "R1 timeline comment: '21-day sale notice'"),
        ("MIN_DAYS_TO_SALE", str(lr.MIN_DAYS_TO_SALE),
         "R1 timeline comment: '= ~41 days minimum' (= CURE_NOTICE_DAYS + "
         "SALE_NOTICE_DAYS)"),
        ("SALE_DAY", str(lr.SALE_DAY),
         "R1 comment: moves to first Wednesday if Jan 1 or Jul 4"),
        ("SALE_TIME_START / SALE_TIME_END",
         f"{lr.SALE_TIME_START} - {lr.SALE_TIME_END}",
         "R1 sale-mechanics comment (first-Tuesday auction window)"),
        ("SALE_MUST_BEGIN_WITHIN_HOURS", str(lr.SALE_MUST_BEGIN_WITHIN_HOURS),
         "R1 sale-mechanics comment"),
        ("MORTGAGE_REDEMPTION", str(lr.MORTGAGE_REDEMPTION),
         "R1 comment: 'Redemption: NONE after mortgage foreclosure — sale is final'"),
        ("EVICTION_NOTICE_DAYS", str(lr.EVICTION_NOTICE_DAYS),
         "R1 comment: post-sale 3-day notice to vacate before eviction"),
    ]

    # --- R2: Tax-foreclosure sale (redeemable tax deed) ---------------
    rows += [
        ("TAX_SALE_STATUTE", str(lr.TAX_SALE_STATUTE),
         "R2: judicial tax-foreclosure sale"),
        ("REDEMPTION_DAYS_HOMESTEAD "
         "(RedemptionClass.HOMESTEAD_AG)", f"{lr.REDEMPTION_DAYS_HOMESTEAD} days",
         "R2 comment: 2 years (homestead/ag/mineral); Tex. Tax Code Ch. 34, §34.21"),
        ("REDEMPTION_DAYS_OTHER "
         "(RedemptionClass.OTHER)", f"{lr.REDEMPTION_DAYS_OTHER} days",
         "R2 comment: non-homestead/ag 180-day redemption; Tex. Tax Code Ch. 34, §34.21"),
        ("REDEMPTION_PREMIUM_YR1", _pct(lr.REDEMPTION_PREMIUM_YR1),
         "R2 comment: 25% if redeemed in year 1 — one-time statutory penalty on "
         "aggregate (bid + recording fee + post-sale taxes/costs)"),
        ("REDEMPTION_PREMIUM_YR2", _pct(lr.REDEMPTION_PREMIUM_YR2),
         "R2 comment: 50% if redeemed in year 2 (2-year class only); "
         "180-day class caps at 25%"),
        ("IRS_REDEMPTION_DAYS", str(lr.IRS_REDEMPTION_DAYS),
         "R2 comment: IRS federal tax lien — 120-day right of redemption"),
    ]

    # --- R3: Non-disclosure state --------------------------------------
    rows.append(
        ("NON_DISCLOSURE", str(lr.NON_DISCLOSURE),
         "R3: Texas non-disclosure state; " + "; ".join(lr.NON_DISCLOSURE_STATUTES)),
    )

    # --- R4: Wholesaling / assignment legality -------------------------
    rows += [
        ("WHOLESALE_BILATERAL_DISCLOSURE", str(lr.WHOLESALE_BILATERAL_DISCLOSURE),
         "R4 comment: bilateral disclosure mandatory since Jan 1, 2024"),
        ("WHOLESALE_DISCLOSURE_REQUIRED_BEFORE",
         str(lr.WHOLESALE_DISCLOSURE_REQUIRED_BEFORE),
         "R4: disclosure deadline — before contracting"),
        ("WHOLESALE_STATUTES", "; ".join(lr.WHOLESALE_STATUTES),
         "R4 statute list (incl. SB 2212 (2017), SB 1577 eff. Jan 1, 2024)"),
        ("WHOLESALE_PROHIBITED", "; ".join(lr.WHOLESALE_PROHIBITED),
         "R4 comment: prohibited acts make it unlicensed brokerage — Class A misdemeanor"),
    ]

    # --- R5: Purchase & option mechanics -------------------------------
    rows.append(
        ("EARNEST_MONEY_DAYS", str(lr.EARNEST_MONEY_DAYS),
         "R5 comment: deliver to escrow within 3 days of effective date; option fee "
         "buys unrestricted right to terminate during option period"),
    )

    # --- County-specific buyer eligibility -----------------------------
    for county, spec in lr.BUYER_ELIGIBILITY.items():
        cost = spec.get("cost")
        cost_txt = f"${cost}" if cost is not None else "TBD"
        rows.append(
            (f"BUYER_ELIGIBILITY[{county.name}]",
             f"{spec.get('requirement')}; cost={cost_txt}; source={spec.get('source')}",
             "County-specific buyer eligibility (R6 area); Grayson entry is "
             "'TBD — verify in WS-00'"),
        )
    return rows


def _redemption_quick_reference() -> str:
    """Live function-call results for the two redemption classes (+ IRS)."""
    hc, ot = lr.RedemptionClass.HOMESTEAD_AG, lr.RedemptionClass.OTHER
    lines = [
        "| Call | Result |",
        "|------|--------|",
        f"| `get_redemption_days(RedemptionClass.HOMESTEAD_AG)` | "
        f"{lr.get_redemption_days(hc)} days |",
        f"| `get_redemption_days(RedemptionClass.OTHER)` | "
        f"{lr.get_redemption_days(ot)} days |",
        f"| `get_redemption_premium(RedemptionClass.HOMESTEAD_AG, year=1)` | "
        f"{lr.get_redemption_premium(hc, 1):.0%} |",
        f"| `get_redemption_premium(RedemptionClass.HOMESTEAD_AG, year=2)` | "
        f"{lr.get_redemption_premium(hc, 2):.0%} |",
        f"| `get_redemption_premium(RedemptionClass.OTHER, year=1)` | "
        f"{lr.get_redemption_premium(ot, 1):.0%} |",
        f"| `get_redemption_premium(RedemptionClass.OTHER, year=2)` (caps) | "
        f"{lr.get_redemption_premium(ot, 2):.0%} |",
        f"| `IRS_REDEMPTION_DAYS` (federal tax lien overlay) | {lr.IRS_REDEMPTION_DAYS} days |",
    ]
    return "\n".join(lines)


def generate(out_path: str = DEFAULT_OUT, today: str | None = None) -> str:
    """Render the markdown package and atomically write it to *out_path*."""
    today = today or datetime.now().date().isoformat()
    version = lr.get_version_info()
    md: list[str] = [
        "# LEGAL VERIFICATION PACKAGE — HERMES-REO",
        "",
        f"**Generated:** {today}",
        f"**Source module:** `src/legal_rules.py` v{version['version']} · "
        f"last_verified {version['last_verified']} · verified_by "
        f"`{version['verified_by']}`",
        "**Purpose:** attorney review and sign-off of every legal-outcome "
        "constant the system enforces. Nothing here is counsel-approved until "
        "countersigned below.",
        "",
        "> **STATUS: ALL ROWS UNVERIFIED** — constants were set by "
        f"`{lr.VERIFIED_BY}` and drive live decisions (redemption calendars, "
        "disclosure gates, auction eligibility). An incorrect value here is a "
        "legal defect in every deal underwritten after it.",
        "",
        "## Constants for attorney verification",
        "",
        "| # | Constant | Current Value | Source Citation (per src/legal_rules.py "
        "comments/statutes) | verified_by | Attorney Notes |",
        "|---|----------|---------------|----------------------------------------------"
        "-------------|-------------|----------------|",
    ]
    for i, (name, value, citation) in enumerate(_rows(), start=1):
        notes = ""  # deliberately blank — filled in by the reviewing attorney
        md.append(
            f"| {i} | `{name}` | {value} | {citation} | {_status()} | {notes} |"
        )
    md += [
        "",
        "### Redemption-class quick reference (live function results)",
        "",
        _redemption_quick_reference(),
        "",
        "### Cross-reference: offer-term economics NOT in legal_rules.py",
        "",
        "Earnest/option dollar minimums and timelines live in `src/policy.py`, "
        "not `src/legal_rules.py`: `EARNEST_MONEY_PCT`/`EARNEST_MONEY_MIN`, "
        "`OPTION_FEE_PCT`/`OPTION_FEE_MIN`, `OPTION_PERIOD_DAYS`/"
        "`OPTION_PERIOD_DAYS_EXTENDED`, `CLOSING_TIMELINE_DAYS`/"
        "`CLOSING_TIMELINE_DAYS_MAX`. Verify contract-drafting impact against "
        "the TREC 1-4 Family contract the system will actually use.",
        "",
        "## Attorney sign-off",
        "",
        "- Attorney name / Bar Card No.: ____________________",
        "- Signature: ____________________  Date: __________",
        "- Scope reviewed: entire table above (all rows).",
        "- Corrections: mark directly in Attorney Notes; corrected values must "
        "be committed to `src/legal_rules.py` with `VERIFIED_BY`/`LAST_VERIFIED` "
        "updated in the same change.",
        "",
    ]
    write_text(out_path, "\n".join(md))
    return out_path


def self_check() -> None:
    """Generate to a temp dir; assert content invariants; clean up."""
    tmpdir = tempfile.mkdtemp(prefix="legal_pkg_selfcheck_")
    try:
        out_path = os.path.join(tmpdir, "LEGAL_VERIFICATION_PACKAGE.md")
        generate(out_path, today="2026-08-25")
        with open(out_path, encoding="utf-8") as f:
            text = f.read()
        assert text.strip(), "generated package is empty"
        # RedemptionClass rows must be present (both classes).
        assert "RedemptionClass.HOMESTEAD_AG" in text, "missing HOMESTEAD_AG row"
        assert "RedemptionClass.OTHER" in text, "missing OTHER row"
        assert "IRS_REDEMPTION_DAYS" in text, "missing IRS 120-day row"
        # At least 8 numbered constant rows.
        n_rows = len(re.findall(r"^\| \d+ \| `", text, flags=re.MULTILINE))
        assert n_rows >= 8, f"expected >= 8 constant rows, found {n_rows}"
        print(f"gen_legal_package.py: self-check passed ({n_rows} constant rows, "
              f"RedemptionClass rows present)")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/gen_legal_package.py",
        description="Generate the TX attorney verification package from "
                    "src/legal_rules.py constants.",
    )
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help=f"output path (default: {DEFAULT_OUT})")
    parser.add_argument("--self-check", action="store_true",
                        help="generate to a temp dir and assert content instead "
                             "of writing the real package")
    args = parser.parse_args(argv)

    if args.self_check:
        self_check()
        return 0
    out_path = generate(args.out)
    print(f"Legal verification package written: {out_path}")
    print(f"All rows carry verified_by='{lr.VERIFIED_BY}' — route to TX RE "
          f"attorney for countersignature.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
