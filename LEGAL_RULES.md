# LEGAL-RULES v1 — Texas Real Estate Distressed Acquisition Rules

**Version:** 1.0
**Last verified:** 2026-08-19 (spec research; pending TX RE attorney confirmation)
**Jurisdiction:** Texas — Collin, Dallas, Grayson Counties

> ⚠️ **This is an engineering artifact, not legal advice.** Every rule below
> carries `verified_by: "spec_research"` until a Texas real estate attorney
> confirms. The system must treat these as versioned and re-verifiable.

---

## R1 — Mortgage Foreclosure (Non-Judicial Trustee Sale)

| Field | Value |
|-------|-------|
| **rule_id** | `R1_MORTGAGE_FORECLOSURE` |
| **statute_ref** | Tex. Prop. Code §51.002 (Ch. 51 generally) |
| **type** | Non-judicial (power-of-sale clause in deed of trust) |
| **verified_by** | spec_research |
| **last_verified** | 2026-08-19 |
| **version** | 1.0 |

### Timeline
- Federal servicing rule: first foreclosure filing barred until loan >120 days delinquent
- Notice of Default & Intent to Accelerate: ≥20 days to cure
- Notice of (Substitute) Trustee's Sale: certified mail + filed with county clerk + posted at courthouse ≥21 days before sale
- **Minimum ~41 days** from first notice to sale
- One of the fastest foreclosure states

### Sale mechanics
- **When:** First Tuesday of the month (moves to first Wednesday if Jan 1 or Jul 4)
- **Time:** Between 10:00 a.m. and 4:00 p.m.; sale must begin within 3 hours of posted time
- **Where:** Commissioners-court-designated area at/near county courthouse
  - Collin: 2100 Bloomdale Rd, McKinney, TX 75071 (courthouse steps) — confirmed live
  - Dallas: North side of George Allen Courts Building — confirmed live
  - Grayson: West door of courthouse — confirmed live
- **Payment:** Certified/cash funds, no financing, no warranty, no title insurance at sale
- **Bidding:** Lender typically opens with credit bid up to debt amount

### Redemption
- **None** after non-judicial mortgage foreclosure. Sale is final.
- (Contrast with tax sales — R2 below)

### Lien priority
- Sale extinguishes junior liens
- **Senior liens survive** — buying a second-lien foreclosure leaves the senior mortgage in place
- **Property-tax liens can survive** a mortgage foreclosure
- HOA and other liens may survive
- → Mandatory title/lien check before any bid (GATE-LEGAL-LIEN)

### Post-sale
- New owner must give occupants 3 days' notice to vacate before eviction
- Deficiency judgments possible (separate action, Prop. Code §51.003)

### Data note
- Loan balance is NOT public and NOT on the notice
- Original loan amount + origination date inferrable from recorded deed of trust
- Some foreclosure-report vendors surface these

---

## R2 — Tax-Foreclosure Sale (Redeemable Tax Deed)

| Field | Value |
|-------|-------|
| **rule_id** | `R2_TAX_FORECLOSURE` |
| **statute_ref** | Tex. Tax Code Ch. 34, redemption §34.21 |
| **type** | Judicial (taxing unit sues, obtains judgment, sheriff/constable sells) |
| **verified_by** | spec_research |
| **last_verified** | 2026-08-19 |
| **version** | 1.0 |

### What you buy
- You buy the **property**, not a lien certificate
- Texas is a **redeemable deed** state, not a tax-lien-certificate state
- Winning bidder pays in full, certified funds, day of sale
- Receives sheriff's/constable's deed — **without warranty**

### Redemption (Tax Code §34.21)

| Property class | Redemption period | Premium if redeemed yr1 | Premium if redeemed yr2 |
|---------------|-------------------|------------------------|------------------------|
| Homestead / agricultural / mineral | **2 years** from deed recording date | 25% of aggregate | 50% of aggregate |
| All other (rental, commercial, vacant) | **180 days** | 25% of aggregate | N/A (caps at 25%) |

- **Aggregate** = bid + recording fee + post-sale taxes/costs
- Premium is a **one-time statutory penalty**, not annualized
- Redemption yield = your guaranteed return if the property redeems

### Title reality
- Purchase does **NOT** confer clear, insurable title
- During redemption: you hold deed + possession rights, but should not make irreversible improvements
- After redemption expires: file **quiet-title action** (TX RE attorney) for marketable/insurable title

### Surviving-lien traps
- Tax sale extinguishes most liens, BUT:
  - **IRS/federal tax liens:** 120-day federal right of redemption
  - Some **HOA** liens can survive
  - **Municipal** liens (weed/demolition) can survive
  - **Environmental** liens can survive
- Title search is non-negotiable

### Struck-off pool
- Parcels drawing no acceptable bid are "struck off" to taxing units
- Later resold via sealed bid / resale
- Public, free, thin in wealthy DFW counties but occasionally usable

### Buyer eligibility (county-specific)

| County | Requirement | Cost | Source |
|--------|------------|------|--------|
| Collin | Notarized Written Statement declaring no delinquent tax owed in county | ~$10 | Tax Assessor office — confirmed live |
| Dallas | Register with RealAuction.com (online auction) | TBD | dallas.texas.sheriffsaleauctions.com — confirmed live |
| Grayson | TBD — verify in WS-00 | TBD | Sheriff's office |

---

## R3 — Non-Disclosure State

| Field | Value |
|-------|-------|
| **rule_id** | `R3_NON_DISCLOSURE` |
| **statute_ref** | Gov. Code §552.149; Tax Code §22.27 |
| **verified_by** | spec_research |
| **last_verified** | 2026-08-19 |
| **version** | 1.0 |

### Rule
- Sale prices are **NOT public** in Texas
- Not recorded on the deed, not searchable at the clerk, not compellable by CADs
- Only buyer, seller, and their agents know the number

### Consequence
- Public-records-only and consumer-AVM ARVs are unreliable (10%+ error typical)
- This is the **primary source of catastrophic overpay** and the exact "confident-but-wrong" signal to reject

### What IS available
- Listing price
- CAD assessed/appraised value (lags market, sales-chased)
- Ownership, characteristics
- Mortgage amount on recorded warranty deed / deed of trust (reverse-engineer approximate value via assumed LTV — weak signal, low confidence)

### Approved comp provenance sources (confidence order)

| Tier | Source | Rigor | Cost | Use for |
|------|--------|-------|------|---------|
| 3 | MLS via licensed agent/broker | Highest | Relationship | T2/T3 deals; sold prices load within days |
| 2 | Licensed appraiser / BPO | High | Per-deal fee | T3 deals; highest rigor |
| 1 | Title company sale data | Medium | ~$100-250 | Sales history |
| 0 | Paid vendor AVM (ATTOM, HouseCanary, etc.) | Low | Subscription | Directional only; never sole basis |

### Design implication
- Valuation is a **licensed-human-in-the-loop or paid-feed function**
- A pure scraper pipeline cannot produce a usable ARV
- → GATE-VAL enforces this (see IMPLEMENTATION_PLAN.md §5.1)

---

## R4 — Wholesaling / Assignment Legality

| Field | Value |
|-------|-------|
| **rule_id** | `R4_WHOLESALE` |
| **statute_ref** | Occ. Code §1101.0045, §1101.351; Prop. Code §5.0205; SB 2212 (2017), SB 1577 (eff. Jan 1, 2024) |
| **verified_by** | spec_research |
| **last_verified** | 2026-08-19 |
| **version** | 1.0 |

### Legal without a license IF done correctly
- You may acquire an option or equitable interest in a purchase contract and assign/sell that contract for a fee

### Bilateral written disclosure (MANDATORY since Jan 1, 2024 / SB 1577)
- **Before contracting**, you must disclose in writing to BOTH:
  - The seller
  - The potential buyer
- That you are selling only an option / assigning a contract and do not hold legal title
- (Formerly buyer-side only; now bilateral)

### Prohibited (makes it unlicensed brokerage — Class A misdemeanor, Occ. Code §1101.351)
- Listing the property on MLS
- Representing/negotiating on behalf of either party as their agent
- Marketing the property as if you own it
- You market the **contract/equitable interest**, not the house

### Contract mechanics
- Include "and/or assigns" by the buyer name
- Use assignable earnest-money contracts
- Line up wholesaler-friendly title companies experienced with assignments and double-closes

### Tax
- Assignment fee is ordinary income (federal)
- No TX state income tax on individuals, but federal applies

### Watch item
- TREC has an open study on wholesaling — rule may tighten
- LEGAL-RULES module must be re-verified each session before generating any marketing or disclosure text

---

## R5 — Standard Purchase & Option Mechanics

| Field | Value |
|-------|-------|
| **rule_id** | `R5_PURCHASE_OPTION` |
| **statute_ref** | TREC One-to-Four Family Residential Contract (Resale) |
| **verified_by** | spec_research |
| **last_verified** | 2026-08-19 |
| **version** | 1.0 |

### Earnest money + option fee
- Buyer delivers earnest money + option fee to escrow within **3 days** of effective date
- The option fee buys an **unrestricted right to terminate** during the negotiated option period
- If no option fee/period is set, no unrestricted termination right

### Special authorizations
- Certain items (e.g., hydrostatic testing) require separate written seller authorization

### Mapping to transaction gates
- GATE-TXN-EARNEST: verify earnest + option fee in escrow within 3 days
- GATE-TXN-OPTION: track option period deadline (hard date)
- GATE-TXN-TITLE: title commitment ordered, lien objections cleared or priced

---

## R6 — Deal-Type Risk Matrix

| Deal type | Entry channel | Key TX risk | Redemption | Title at purchase |
|-----------|--------------|-------------|-------------|-------------------|
| DT-PREFORE | Trustee-sale notices, tax-suit filings, lis pendens, code violations, probate | Speed (~41-day clock); loan balance not public | N/A (pre-foreclosure) | N/A |
| DT-AUCTION | County foreclosure-notice portals (≥21 days ahead) | Occupied → eviction (3-day notice); sight-unseen | None (mortgage foreclosure) | No title insurance at sale |
| DT-REO | Post-auction lender inventory, MLS | Often near retail — margin from condition/DOM | N/A | Title insurance available |
| DT-TAXDEED | Constable/sheriff sales, RealAuction (Dallas), struck-off/resale | No clear title; no irreversible improvements during redemption | 180d (non-homestead) or 2yr (homestead/ag) | No; quiet-title after redemption |
| DT-SHORT | Listed short sales, underwater sellers | Lender approval timeline/uncertainty | N/A | Standard |
| DT-WHOLESALE | Any of above where exit = assign | Bilateral disclosure mandatory; licensing boundary | Per underlying deal type | Per underlying deal type |
| DT-ONMARKET | MLS (via agent), FSBO | Standard | N/A | Standard |
| DT-OFFMARKET | Off-market lead lists | Standard | N/A | Standard |

---

## Versioning Protocol

1. Before any compliance/legal text generation, the system calls `legal_rules.get_version()` and logs it
2. Rules with `verified_by: "spec_research"` are flagged as unverified in all outputs
3. When a TX RE attorney confirms a rule, update `verified_by` to their name + `last_verified` date
4. When statutes change (e.g., TREC wholesaling study outcome), increment version and update text
5. The LEGAL-RULES module is the single source of truth — never inline these constants in business logic