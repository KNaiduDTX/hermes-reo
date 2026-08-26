# tests/test_notice_parsing.py
"""NO-FABRICATION contract pins for the two foreclosure-notice parsers (FN5/TD6).

Pins ``src/adapters/dallas_foreclosure.parse_notice_text`` on realistic
notice fixtures authored here, plus parity spot-cases against the duplicate
engine in ``src/adapters/notice_ocr.parse_notice_text`` (finding TD6: two
parsers carry the same E2 rules — these tests fail loudly if they drift).

Contract under test (finding E2):
- estimated_default_balance comes ONLY from labeled amounts; unlabeled
  dollar figures stay candidates in all_extracted_balances and the record
  status downgrades to Partial/NoDebt instead of inventing a balance.
- sale_date comes ONLY from an explicitly stated date — the old
  "First Tuesday of {month}" invention must never reappear (None when
  unstated/unparseable).
- lenders are verbatim only: no guessed brands, no appended legal
  suffixes; "NOMINEE FOR <beneficiary>" resolves verbatim.
"""
from datetime import date

import pytest

from src.dates import parse_date_any


# --- lazy adapter accessors (nothing network/browser runs at collection) ----

def _dallas():
    """Import the Dallas adapter lazily; skip cleanly without bs4/pypdf."""
    pytest.importorskip("bs4")
    pytest.importorskip("pypdf")
    from src.adapters import dallas_foreclosure
    return dallas_foreclosure


def _ocr():
    """Import the OCR parser lazily (stdlib-only, kept uniform with above)."""
    from src.adapters import notice_ocr
    return notice_ocr


# --- realistic notice fixtures (authored; shaped like county postings) ------

NOTICE_FULL_LABELED = """
NOTICE OF SUBSTITUTE TRUSTEE'S SALE

NOTICE IS HEREBY GIVEN that the property described below will be sold at
public auction to the highest bidder for cash on
Tuesday, the 1st day of September, 2026, at 10:00 AM

LEGAL DESCRIPTION: Lot 12, Block B, Elm Grove Addition No. 3,
City of Mesquite, Dallas County, Texas
Property Address: 814 PINEHURST DRIVE, MESQUITE, TX 75149
Grantor(s): JOHN PAUL DOE AND JANE ROE DOE
Current Mortgagee: WELLS FARGO BANK, N.A.
Substitute Trustee: MACKIE WOLF ZIENTZ & MANN, P.C.
Original Principal and interest secured hereby: $250,000.00
"""

NOTICE_MERS_NOMINEE = """
NOTICE OF SUBSTITUTE TRUSTEE'S SALE

Property Address: 4021 MOCKINGBIRD LANE, DALLAS, TX 75220
Grantor(s): SALLY Q PUBLIC
Lender/Beneficiary: MERS (MORTGAGE ELECTRONIC REGISTRATION SYSTEMS, INC.)
acting solely as NOMINEE FOR JPMORGAN CHASE BANK, N.A., its successors
and assigns, did on March 14, 2019 execute a Deed of Trust
Unpaid Balance of the Note plus protective advances: $148,332.19
"""

NOTICE_UNLABELED_AMOUNTS = """
NOTICE OF SUBSTITUTE TRUSTEE'S SALE

Property Address: 1204 ROSEMONT AVENUE, DALLAS, TX 75208
Deed of Trust recorded as Instrument Number 201900123456 in the
Official Public Records of Dallas County, Texas.
Filing fee paid in the amount of $7,500.00. Advertising costs of
$1,200.00 were advanced by the trustee. Servicing systems are
operated on platforms associated with Rocket Mortgage brands.
"""

NOTICE_NO_DATE_STATED = """
NOTICE OF SUBSTITUTE TRUSTEE'S SALE

Property Address: 77 CEDAR SPRINGS ROAD, DALLAS, TX 75201
Current Mortgagee: FIRST NATIONAL BANKING CORPORATION
Debt Amount: $310,450.00
The sale will be conducted at the place designated by the county.
"""

NOTICE_SPARSE = (
    "NOTICE OF SALE\n"
    "Boilerplate recitals without figures, parties, or places of any kind.\n"
)

# Address-shape sample 1: labeled prefix (city from comma-separated form).
NOTICE_ADDR_LABELED_PREFIX = NOTICE_FULL_LABELED

# Address-shape sample 2: no prefix — recovered from the ZIP-anchored lines.
NOTICE_ADDR_ZIP_ANCHORED = """
NOTICE OF SALE
The homestead located at
1019 INTREPID CT
Desoto, TX 75115
will be sold at public auction.
"""

# Address-shape sample 3: no prefix, no ZIP — street-pattern fallback.
NOTICE_ADDR_STREET_PATTERN = """
NOTICE OF SALE
the undersigned will offer for sale the property
commonly known as 217 RAVENWOOD LANE, GARLAND, TEXAS
to satisfy the debt secured thereby.
"""


# --- dallas_foreclosure.parse_notice_text -----------------------------------

class TestDallasNoFabrication:
    def test_labeled_debt_is_extracted(self):
        result = _dallas().parse_notice_text(
            NOTICE_FULL_LABELED, "Mesquite", "September")

        assert result["debt"] == 250_000.0
        assert result["all_extracted_balances"] == [250_000.0]

    def test_stated_sale_date_and_time_are_verbatim(self):
        result = _dallas().parse_notice_text(
            NOTICE_FULL_LABELED, "Mesquite", "September")

        assert result["sale_date"] == \
            "Tuesday, the 1st day of September, 2026"
        # The stated date must stay machine-parseable downstream (X4 chain).
        assert parse_date_any(result["sale_date"]) == date(2026, 9, 1)
        assert result["sale_time"] == "10:00 AM"

    def test_unlabeled_amounts_never_become_debt(self):
        result = _dallas().parse_notice_text(
            NOTICE_UNLABELED_AMOUNTS, "Dallas", "November")

        # Fees/costs stay review candidates only — never a balance.
        assert result["debt"] is None
        assert result["all_extracted_balances"] == [7_500.0, 1_200.0]
        # Brand mention alone never becomes the foreclosing lender.
        assert result["lender"] is None
        assert result["sale_date"] is None and result["sale_time"] is None

    def test_sparse_notice_keeps_key_shape_with_none_values(self):
        result = _dallas().parse_notice_text(NOTICE_SPARSE, "Dallas",
                                             "September")

        expected_keys = {"property_address", "city", "borrower", "lender",
                         "debt", "all_extracted_balances", "sale_date",
                         "sale_time", "trustee"}
        assert set(result.keys()) == expected_keys
        assert result["debt"] is None
        assert result["sale_date"] is None
        assert result["sale_time"] is None
        assert result["lender"] is None
        assert result["property_address"] == "Unknown"

    def test_first_tuesday_invention_absent_when_no_date_stated(self):
        """Even with debt+lender present, an unstated date stays None."""
        result = _dallas().parse_notice_text(
            NOTICE_NO_DATE_STATED, "Dallas", "September")

        assert result["debt"] == 310_450.0          # other fields still work
        assert result["sale_date"] is None
        assert result["sale_time"] is None
        assert "First Tuesday" not in str(result.values())


class TestDallasLenderVerbatim:
    def test_notice_supplied_suffix_is_kept_not_extended(self):
        result = _dallas().parse_notice_text(
            NOTICE_FULL_LABELED, "Mesquite", "September")

        assert result["lender"] == "WELLS FARGO BANK, N.A."

    def test_mers_nominee_resolves_to_beneficiary_verbatim(self):
        result = _dallas().parse_notice_text(
            NOTICE_MERS_NOMINEE, "Dallas", "October")

        assert result["lender"] == "JPMORGAN CHASE BANK"
        assert result["debt"] == 148_332.19

    def test_brand_mention_alone_yields_no_lender(self):
        text = ("NOTICE OF SALE\n"
                "Loan servicing systems by Rocket Mortgage and PennyMac.\n"
                "PROPERTY ADDRESS: 9 ELM STREET, DALLAS, TX 75201\n")
        result = _dallas().parse_notice_text(text, "Dallas", "September")

        assert result["lender"] is None
        assert "N.A." not in str(result["lender"])


class TestDallasAddressAndCity:
    def test_sample_labeled_prefix_address_with_city(self):
        result = _dallas().parse_notice_text(
            NOTICE_ADDR_LABELED_PREFIX, "Mesquite", "September")

        assert result["property_address"] == \
            "814 PINEHURST DRIVE, MESQUITE, TX 75149"
        assert result["city"] == "MESQUITE"

    def test_sample_zip_anchored_address_without_prefix(self):
        result = _dallas().parse_notice_text(
            NOTICE_ADDR_ZIP_ANCHORED, "DeSoto", "July")

        assert result["property_address"] == \
            "1019 INTREPID CT Desoto, TX 75115"
        assert result["city"] == "DeSoto"

    def test_sample_street_pattern_fallback_without_zip(self):
        result = _dallas().parse_notice_text(
            NOTICE_ADDR_STREET_PATTERN, "Garland", "August")

        assert result["property_address"] == "217 RAVENWOOD LANE, GARLAND"
        assert result["city"] == "Garland"

    def test_borrower_and_trustee_extracted_verbatim(self):
        result = _dallas().parse_notice_text(
            NOTICE_FULL_LABELED, "Mesquite", "September")

        assert result["borrower"] == "JOHN PAUL DOE AND JANE ROE DOE"
        assert result["trustee"] == "MACKIE WOLF ZIENTZ & MANN, P.C."


class TestDallasRecordStatuses:
    """process_dallas_pdf maps extraction quality onto Success/Partial/NoDebt."""

    class _FakePage:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    @staticmethod
    def _run(tmp_path, monkeypatch, notice_text):
        df = _dallas()
        stub_pdf = tmp_path / "stub.pdf"
        monkeypatch.setattr(df, "download_pdf",
                            lambda url, directory, **kw: str(stub_pdf))
        monkeypatch.setattr(
            df, "split_pdf_pages",
            lambda path: [[TestDallasRecordStatuses._FakePage(notice_text)]])
        records = df.process_dallas_pdf(
            {"url": "https://www.dallascounty.org/f/Dallas_x.pdf",
             "filename": "Dallas_x.pdf", "city": "Dallas",
             "month": "September"},
            str(tmp_path))
        assert len(records) == 1
        return df, records[0]

    def test_full_labeled_record_reports_success(self, tmp_path, monkeypatch):
        df, record = self._run(tmp_path, monkeypatch, NOTICE_FULL_LABELED)

        assert record["entry_id"] == "DAL_September_Dallas_1"
        assert record["county"] == "Dallas"
        ocr_block = record["details"]["scanned_notice_ocr_data"]
        assert {"foreclosing_lender", "substitute_trustee",
                "scheduled_sale_time", "estimated_default_balance",
                "all_extracted_balances", "ocr_engine_mode",
                "status"} <= set(ocr_block.keys())
        assert ocr_block["estimated_default_balance"] == 250_000.0
        assert ocr_block["status"] == "Success"

    def test_unlabeled_amounts_downgrade_status_to_partial(
            self, tmp_path, monkeypatch):
        _, record = self._run(tmp_path, monkeypatch, NOTICE_UNLABELED_AMOUNTS)

        ocr_block = record["details"]["scanned_notice_ocr_data"]
        assert ocr_block["estimated_default_balance"] is None
        assert ocr_block["all_extracted_balances"] == [7_500.0, 1_200.0]
        assert ocr_block["status"] == "Partial"

    def test_spare_record_reports_nodebt_without_inventing_values(
            self, tmp_path, monkeypatch):
        _, record = self._run(tmp_path, monkeypatch, NOTICE_SPARSE)

        ocr_block = record["details"]["scanned_notice_ocr_data"]
        assert ocr_block["estimated_default_balance"] is None
        assert ocr_block["foreclosing_lender"] is None
        assert ocr_block["scheduled_sale_time"] is None
        assert ocr_block["status"] == "NoDebt"


# --- parity spot-cases: dallas_foreclosure vs notice_ocr (TD6) --------------

@pytest.mark.parametrize(
    "notice_text",
    [NOTICE_FULL_LABELED, NOTICE_MERS_NOMINEE, NOTICE_UNLABELED_AMOUNTS],
    ids=["full-labeled", "mers-nominee", "unlabeled-amounts"],
)
def test_parser_parity_core_fields(notice_text):
    dal = _dallas().parse_notice_text(notice_text, "Dallas", "September")
    ocr = _ocr().parse_notice_text(notice_text)

    assert dal["lender"] == ocr.foreclosing_lender
    assert dal["debt"] == ocr.estimated_default_balance
    assert dal["sale_time"] == ocr.scheduled_sale_time
    assert dal["all_extracted_balances"] == ocr.all_extracted_balances


# --- notice_ocr.parse_notice_text statuses -----------------------------------

def test_ocr_success_on_labeled_notice():
    data = _ocr().parse_notice_text(NOTICE_FULL_LABELED)

    assert data.status == "success"
    assert data.estimated_default_balance == 250_000.0
    assert data.original_principal == 250_000.0
    assert data.foreclosing_lender == "WELLS FARGO BANK, N.A."
    assert data.substitute_trustee == "MACKIE WOLF ZIENTZ & MANN, P.C."


def test_ocr_partial_keeps_candidates_but_no_balance():
    data = _ocr().parse_notice_text(NOTICE_UNLABELED_AMOUNTS)

    assert data.status == "partial"
    assert data.estimated_default_balance is None
    assert data.original_principal is None
    assert data.unpaid_principal_balance is None
    assert data.scheduled_sale_time is None
    assert data.foreclosing_lender is None
    assert data.all_extracted_balances == [7_500.0, 1_200.0]


@pytest.mark.parametrize(
    "text, expected_status",
    [
        ("no data here", "no_debt_found"),
        ("NOTICE OF SALE\nFiling fee $7,500.00 only.\n", "partial"),
        ("   ", "no_text"),
        ("", "no_text"),
    ],
    ids=["nothing-extractable", "candidate-only", "whitespace", "empty"],
)
def test_ocr_statuses(text, expected_status):
    data = _ocr().parse_notice_text(text)

    assert data.status == expected_status
    assert data.estimated_default_balance is None
