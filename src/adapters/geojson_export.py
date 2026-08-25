# src/adapters/geojson_export.py
"""GeoJSON point map export — PORTED from lucid-noether export_geojson.py.

Exports foreclosure properties as GeoJSON points with investment categories,
marker colors/symbols, and census tract overlay for field operations.
"""
from __future__ import annotations
import re
import statistics
from ..io_utils import write_json
from .route_optimizer import RouteStop


def _parse_value(val) -> float:
    if val is None or val == "Unknown":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = re.sub(r'[^\d.]', '', str(val))
    return float(cleaned) if cleaned else 0.0


def cpr_to_geojson(cprs: list) -> dict:
    """Export CPRs as a GeoJSON FeatureCollection.

    Each CPR becomes a Point feature with properties for map display.
    """
    features = []
    city_pps = {}  # city → [price_per_sqft values]

    # First pass: collect price/sqft for city medians
    for cpr in cprs:
        lat = cpr.gis_lat
        lng = cpr.gis_lng
        if not lat or not lng or lat == 0 or lng == 0:
            continue
        val = cpr.cad_appraised_value.value if cpr.cad_appraised_value else 0.0
        sqft = cpr.improvement_sqft.value if cpr.improvement_sqft else 0
        if val and val > 0 and sqft and sqft > 0:
            from ..synthesis import get_city
            city = get_city(cpr.situs_address)
            city_pps.setdefault(city, []).append(val / sqft)

    city_medians = {c: statistics.median(pps) for c, pps in city_pps.items() if pps}

    for cpr in cprs:
        lat = cpr.gis_lat
        lng = cpr.gis_lng
        if not lat or not lng or lat == 0 or lng == 0:
            continue

        from ..synthesis import get_city
        city = get_city(cpr.situs_address)
        val = cpr.cad_appraised_value.value if cpr.cad_appraised_value else 0.0
        sqft = cpr.improvement_sqft.value if cpr.improvement_sqft else 0
        pps = val / sqft if (val and val > 0 and sqft and sqft > 0) else None

        city_med = city_medians.get(city, 0)
        pps_discount = ((city_med - pps) / city_med * 100) if (pps and city_med and city_med > 0) else 0.0

        # Investment category
        is_corporate = bool(re.search(
            r'\b(LLC|LP|TRUST|CORP|INC|HOLDINGS|PARTNERS|BANK|HOA|LTD|LLP)\b',
            str(cpr.owner_name.value if cpr.owner_name else ""), re.IGNORECASE
        ))
        if is_corporate or (val and val >= 1_000_000):
            category = "Commercial"
            color = "#e74c3c"
            symbol = "briefcase"
        elif pps_discount > 10:
            category = "Discount Flip"
            color = "#2ecc71"
            symbol = "star"
        elif val and 150_000 <= val <= 550_000:
            category = "Rental"
            color = "#e67e22"
            symbol = "home"
        else:
            category = "Standard"
            color = "#95a5a6"
            symbol = "danger"

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": {
                "title": cpr.situs_address,
                "address": cpr.situs_address,
                "city": city,
                "county": cpr.county,
                "category": category,
                "marker-color": color,
                "marker-symbol": symbol,
                "marker-size": "medium",
                "appraised_value": val,
                "price_per_sqft": round(pps, 2) if pps else None,
                "market_pps_discount_pct": round(pps_discount, 2),
                "year_built": cpr.year_built.value if cpr.year_built else None,
                "owner": cpr.owner_name.value if cpr.owner_name else "Unknown",
                "deal_type": cpr.deal_type,
                "blast_tier": cpr.blast_tier.name if hasattr(cpr.blast_tier, 'name') else str(cpr.blast_tier),
            },
        })

    return {"type": "FeatureCollection", "features": features}


def export_geojson(cprs: list, output_path: str) -> int:
    """Export CPRs to a GeoJSON file atomically (src.io_utils). Returns feature count."""
    geojson = cpr_to_geojson(cprs)
    write_json(output_path, geojson, default=str)
    return len(geojson["features"])


if __name__ == "__main__":
    # Self-check: export a fixture CPR to GeoJSON
    from ..cpr import CPR, ProvenanceField

    cpr = CPR(parcel_id="test", county="collin",
              situs_address="10022 Plainsman Ln, Frisco, TX 75035",
              gis_lat=33.179, gis_lng=-96.794, deal_type="DT-AUCTION")
    cpr.set_field("improvement_sqft", 4332, "test", 0.95)
    cpr.set_field("cad_appraised_value", 730_673, "test", 0.90)
    cpr.set_field("year_built", 2004, "test", 0.95)
    cpr.set_field("owner_name", "Islam Mazhar", "test", 0.95)

    geojson = cpr_to_geojson([cpr])
    assert geojson["type"] == "FeatureCollection"
    assert len(geojson["features"]) == 1
    props = geojson["features"][0]["properties"]
    assert props["title"] == "10022 Plainsman Ln, Frisco, TX 75035"
    assert props["city"] == "Frisco"
    assert props["appraised_value"] == 730_673
    assert props["price_per_sqft"] is not None
    print(f"geojson_export.py: exported {len(geojson['features'])} feature(s)")
    print(f"  Category: {props['category']}, PPS: ${props['price_per_sqft']:.2f}/sqft")
    print("geojson_export.py: all self-checks passed")