# src/adapters/geojson_export.py
"""GeoJSON point map export — PORTED from lucid-noether export_geojson.py.

Exports foreclosure properties as GeoJSON points with investment categories,
marker colors/symbols, and census tract overlay for field operations.
"""
from __future__ import annotations
import re
import statistics
from typing import Any

from ..cpr import CPR
from ..io_utils import write_json
from .route_optimizer import RouteStop


def _parse_value(val) -> float:
    if val is None or val == "Unknown":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = re.sub(r'[^\d.]', '', str(val))
    return float(cleaned) if cleaned else 0.0


def cpr_to_geojson(cprs: list[CPR]) -> dict[str, Any]:
    """Export CPRs as a GeoJSON FeatureCollection.

    Each CPR becomes a Point feature with properties for map display.

    TD9: the legacy two-pass loop duplicated the coordinate gate and the
    CAD/sqft unwrap (one pass for city medians, one for feature building);
    a single extraction pass now unpacks each coordinate-bearing CPR exactly
    once and the city medians are derived from the collected samples.

    TD1: characteristic reads go through CPR.get(), the provenance-unwrapping
    accessor over declared fields — so receipted gis_lat/gis_lng feed real
    numbers into the gate and geometry instead of leaking the wrapper's
    str() form, matching find_deals.build_geojson_from_deals' row-level
    coordinate predicate exactly.
    """
    from ..synthesis import get_city  # lazy: keeps the adapter import graph light

    # Single extraction pass: one unwrap per CPR, nothing duplicated.
    samples = []  # (cpr, lat, lng, val, sqft, pps, city) — gate-passing only
    for cpr in cprs:
        lat = cpr.get("gis_lat")
        lng = cpr.get("gis_lng")
        if not lat or not lng or lat == 0 or lng == 0:
            continue
        val = cpr.get("cad_appraised_value", 0.0)
        sqft = cpr.get("improvement_sqft", 0)
        pps = val / sqft if (val and val > 0 and sqft and sqft > 0) else None
        samples.append((cpr, lat, lng, val, sqft, pps,
                        get_city(cpr.situs_address)))

    city_pps: dict = {}  # city → [price_per_sqft values]
    for _, _, _, _, _, pps, city in samples:
        if pps is not None:
            city_pps.setdefault(city, []).append(pps)
    city_medians = {c: statistics.median(pps) for c, pps in city_pps.items() if pps}

    features = []
    for cpr, lat, lng, val, sqft, pps, city in samples:
        city_med = city_medians.get(city, 0)
        pps_discount = ((city_med - pps) / city_med * 100) if (pps and city_med and city_med > 0) else 0.0

        # Investment category
        is_corporate = bool(re.search(
            r'\b(LLC|LP|TRUST|CORP|INC|HOLDINGS|PARTNERS|BANK|HOA|LTD|LLP)\b',
            str(cpr.get("owner_name", "")), re.IGNORECASE
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
                "year_built": cpr.get("year_built"),
                "owner": cpr.get("owner_name", "Unknown"),
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
    assert geojson["features"][0]["geometry"]["coordinates"] == [-96.794, 33.179]

    # TD1: provenance-wrapped coordinates unwrap to plain numbers — the gate
    # admits them and the geometry carries [lng, lat] floats, never the
    # ProvenanceField str() form the legacy export serialized.
    cpr.set_field("gis_lat", 33.5, "test", 0.90)
    cpr.set_field("gis_lng", -96.5, "test", 0.90)
    fc_wrapped = cpr_to_geojson([cpr])
    coords = fc_wrapped["features"][0]["geometry"]["coordinates"]
    assert coords == [-96.5, 33.5], coords
    assert all(isinstance(c, float) for c in coords), coords

    # A zeroed receipt is excluded by the same unwrapped gate (no feature).
    cpr.set_field("gis_lat", 0, "test", 0.90)
    assert len(cpr_to_geojson([cpr])["features"]) == 0

    print(f"geojson_export.py: exported {len(geojson['features'])} feature(s)")
    print(f"  Category: {props['category']}, PPS: ${props['price_per_sqft']:.2f}/sqft")
    print("geojson_export.py: TD1 — provenance-wrapped coordinates verified")
    print("geojson_export.py: all self-checks passed")