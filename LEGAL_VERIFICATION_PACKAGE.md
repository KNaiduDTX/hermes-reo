# LEGAL VERIFICATION PACKAGE — HERMES-REO

**Generated:** 2026-08-25
**Source module:** `src/legal_rules.py` v1.0 · last_verified 2026-08-19 · verified_by `spec_research`
**Purpose:** attorney review and sign-off of every legal-outcome constant the system enforces. Nothing here is counsel-approved until countersigned below.

> **STATUS: ALL ROWS UNVERIFIED** — constants were set by `spec_research` and drive live decisions (redemption calendars, disclosure gates, auction eligibility). An incorrect value here is a legal defect in every deal underwritten after it.

## Constants for attorney verification

| # | Constant | Current Value | Source Citation (per src/legal_rules.py comments/statutes) | verified_by | Attorney Notes |
|---|----------|---------------|-----------------------------------------------------------|-------------|----------------|
| 1 | `FORECLOSURE_STATUTE` | Tex. Prop. Code §51.002 | R1: mortgage foreclosure is non-judicial trustee sale | spec_research (UNVERIFIED) |  |
| 2 | `FORECLOSURE_TYPE` | non-judicial | R1 header comment: Mortgage Foreclosure (non-judicial trustee sale) | spec_research (UNVERIFIED) |  |
| 3 | `CURE_NOTICE_DAYS` | 20 | R1 timeline comment: '20-day cure' | spec_research (UNVERIFIED) |  |
| 4 | `SALE_NOTICE_DAYS` | 21 | R1 timeline comment: '21-day sale notice' | spec_research (UNVERIFIED) |  |
| 5 | `MIN_DAYS_TO_SALE` | 41 | R1 timeline comment: '= ~41 days minimum' (= CURE_NOTICE_DAYS + SALE_NOTICE_DAYS) | spec_research (UNVERIFIED) |  |
| 6 | `SALE_DAY` | first_tuesday_of_month | R1 comment: moves to first Wednesday if Jan 1 or Jul 4 | spec_research (UNVERIFIED) |  |
| 7 | `SALE_TIME_START / SALE_TIME_END` | 10:00 - 16:00 | R1 sale-mechanics comment (first-Tuesday auction window) | spec_research (UNVERIFIED) |  |
| 8 | `SALE_MUST_BEGIN_WITHIN_HOURS` | 3 | R1 sale-mechanics comment | spec_research (UNVERIFIED) |  |
| 9 | `MORTGAGE_REDEMPTION` | None | R1 comment: 'Redemption: NONE after mortgage foreclosure — sale is final' | spec_research (UNVERIFIED) |  |
| 10 | `EVICTION_NOTICE_DAYS` | 3 | R1 comment: post-sale 3-day notice to vacate before eviction | spec_research (UNVERIFIED) |  |
| 11 | `TAX_SALE_STATUTE` | Tex. Tax Code Ch. 34, §34.21 | R2: judicial tax-foreclosure sale | spec_research (UNVERIFIED) |  |
| 12 | `REDEMPTION_DAYS_HOMESTEAD (RedemptionClass.HOMESTEAD_AG)` | 730 days | R2 comment: 2 years (homestead/ag/mineral); Tex. Tax Code Ch. 34, §34.21 | spec_research (UNVERIFIED) |  |
| 13 | `REDEMPTION_DAYS_OTHER (RedemptionClass.OTHER)` | 180 days | R2 comment: non-homestead/ag 180-day redemption; Tex. Tax Code Ch. 34, §34.21 | spec_research (UNVERIFIED) |  |
| 14 | `REDEMPTION_PREMIUM_YR1` | 0.25 (25%) | R2 comment: 25% if redeemed in year 1 — one-time statutory penalty on aggregate (bid + recording fee + post-sale taxes/costs) | spec_research (UNVERIFIED) |  |
| 15 | `REDEMPTION_PREMIUM_YR2` | 0.5 (50%) | R2 comment: 50% if redeemed in year 2 (2-year class only); 180-day class caps at 25% | spec_research (UNVERIFIED) |  |
| 16 | `IRS_REDEMPTION_DAYS` | 120 | R2 comment: IRS federal tax lien — 120-day right of redemption | spec_research (UNVERIFIED) |  |
| 17 | `NON_DISCLOSURE` | True | R3: Texas non-disclosure state; Gov. Code §552.149; Tax Code §22.27 | spec_research (UNVERIFIED) |  |
| 18 | `WHOLESALE_BILATERAL_DISCLOSURE` | True | R4 comment: bilateral disclosure mandatory since Jan 1, 2024 | spec_research (UNVERIFIED) |  |
| 19 | `WHOLESALE_DISCLOSURE_REQUIRED_BEFORE` | contracting | R4: disclosure deadline — before contracting | spec_research (UNVERIFIED) |  |
| 20 | `WHOLESALE_STATUTES` | Occ. Code §1101.0045; Occ. Code §1101.351; Prop. Code §5.0205; SB 2212 (2017); SB 1577 (eff. Jan 1, 2024) | R4 statute list (incl. SB 2212 (2017), SB 1577 eff. Jan 1, 2024) | spec_research (UNVERIFIED) |  |
| 21 | `WHOLESALE_PROHIBITED` | mls_listing; representing_party; negotiating_for_party; marketing_as_owned | R4 comment: prohibited acts make it unlicensed brokerage — Class A misdemeanor | spec_research (UNVERIFIED) |  |
| 22 | `EARNEST_MONEY_DAYS` | 3 | R5 comment: deliver to escrow within 3 days of effective date; option fee buys unrestricted right to terminate during option period | spec_research (UNVERIFIED) |  |
| 23 | `BUYER_ELIGIBILITY[COLLIN]` | Written Statement declaring no delinquent tax owed in county; cost=$10; source=Tax Assessor office | County-specific buyer eligibility (R6 area); Grayson entry is 'TBD — verify in WS-00' | spec_research (UNVERIFIED) |  |
| 24 | `BUYER_ELIGIBILITY[DALLAS]` | Register with RealAuction.com (online auction); cost=TBD; source=dallas.texas.sheriffsaleauctions.com | County-specific buyer eligibility (R6 area); Grayson entry is 'TBD — verify in WS-00' | spec_research (UNVERIFIED) |  |
| 25 | `BUYER_ELIGIBILITY[GRAYSON]` | TBD — verify in WS-00; cost=TBD; source=Sheriff's office | County-specific buyer eligibility (R6 area); Grayson entry is 'TBD — verify in WS-00' | spec_research (UNVERIFIED) |  |

### Redemption-class quick reference (live function results)

| Call | Result |
|------|--------|
| `get_redemption_days(RedemptionClass.HOMESTEAD_AG)` | 730 days |
| `get_redemption_days(RedemptionClass.OTHER)` | 180 days |
| `get_redemption_premium(RedemptionClass.HOMESTEAD_AG, year=1)` | 25% |
| `get_redemption_premium(RedemptionClass.HOMESTEAD_AG, year=2)` | 50% |
| `get_redemption_premium(RedemptionClass.OTHER, year=1)` | 25% |
| `get_redemption_premium(RedemptionClass.OTHER, year=2)` (caps) | 25% |
| `IRS_REDEMPTION_DAYS` (federal tax lien overlay) | 120 days |

### Cross-reference: offer-term economics NOT in legal_rules.py

Earnest/option dollar minimums and timelines live in `src/policy.py`, not `src/legal_rules.py`: `EARNEST_MONEY_PCT`/`EARNEST_MONEY_MIN`, `OPTION_FEE_PCT`/`OPTION_FEE_MIN`, `OPTION_PERIOD_DAYS`/`OPTION_PERIOD_DAYS_EXTENDED`, `CLOSING_TIMELINE_DAYS`/`CLOSING_TIMELINE_DAYS_MAX`. Verify contract-drafting impact against the TREC 1-4 Family contract the system will actually use.

## Attorney sign-off

- Attorney name / Bar Card No.: ____________________
- Signature: ____________________  Date: __________
- Scope reviewed: entire table above (all rows).
- Corrections: mark directly in Attorney Notes; corrected values must be committed to `src/legal_rules.py` with `VERIFIED_BY`/`LAST_VERIFIED` updated in the same change.
