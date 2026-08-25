# src/adapters/route_optimizer.py
"""TSP route optimizer for drive-by property inspections — PORTED from lucid-noether.

Nearest-neighbor heuristic + 2-opt improvement. Used for auction prep
(drive-by only, no interior access).
"""
from __future__ import annotations
import math
import re
from dataclasses import dataclass
from typing import Optional

COLLIN_COURTHOUSE = (33.1976, -96.6154)   # 2100 Bloomdale Rd, McKinney
DALLAS_COURTHOUSE = (32.7801, -96.8048)   # George Allen Courts Building
GRAYSON_COURTHOUSE = (33.6407, -96.6061)  # Sherman — approximate coordinates

# E7: explicit county-aware courthouse registry. Grayson's coordinates are
# approximate (courthouse-block centroid in Sherman), and the returned label
# says so instead of implying survey precision.
COUNTY_COURTHOUSES = {
    "dallas": (DALLAS_COURTHOUSE, "Dallas County Courthouse"),
    "collin": (COLLIN_COURTHOUSE, "Collin County Courthouse"),
    "grayson": (GRAYSON_COURTHOUSE,
                "Grayson County Courthouse, Sherman (approximate coordinates)"),
}

# Cities served by each courthouse's county (the DFW counties this repo prices).
_COUNTY_CITIES = {
    "dallas": {
        "dallas", "mesquite", "garland", "irving", "grand prairie", "carrollton",
        "richardson", "rowlett", "cedar hill", "desoto", "lancaster", "duncanville",
        "balch springs", "addison", "farmers branch", "seagoville", "sunnyvale",
        "hutchins", "wilmer", "cockrell hill", "highland park", "university park",
        "glenn heights", "ovilla",
    },
    "collin": {
        "mckinney", "plano", "frisco", "allen", "wylie", "murphy", "prosper",
        "celina", "anna", "melissa", "princeton", "farmersville", "nevada",
        "josephine", "blue ridge", "westminster", "lucas", "fairview", "parker",
        "st paul", "new hope", "lowry crossing",
    },
    "grayson": {
        "sherman", "denison", "van alstyne", "howe", "whitewright", "gunter",
        "bells", "tom bean", "whitesboro", "pottsboro", "southmayd", "sadler",
        "tioga", "collinsville", "pilot grove", "knollwood", "luella",
        "gordonville", "preston",
    },
}
_CITY_TO_COUNTY = {city: county for county, cities in _COUNTY_CITIES.items()
                   for city in cities}


@dataclass
class RouteStop:
    title: str
    address: str
    city: str
    lat: float
    lng: float
    category: str = "Standard"
    value: float = 0.0


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles."""
    R = 3958.8
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = (math.sin(dLat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dLon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def solve_tsp_nn(start: RouteStop, nodes: list[RouteStop]) -> tuple[list[RouteStop], float]:
    """Nearest-neighbor TSP heuristic."""
    unvisited = list(nodes)
    current = start
    route = [start]
    total = 0.0

    while unvisited:
        nearest = min(unvisited, key=lambda n: haversine(current.lat, current.lng, n.lat, n.lng))
        total += haversine(current.lat, current.lng, nearest.lat, nearest.lng)
        route.append(nearest)
        current = nearest
        unvisited.remove(nearest)

    total += haversine(current.lat, current.lng, start.lat, start.lng)
    route.append(start)
    return route, total


def optimize_2opt(route: list[RouteStop], max_iter: int = 500) -> tuple[list[RouteStop], float]:
    """2-opt improvement on a TSP route."""
    best = list(route)
    n = len(best)
    if n <= 4:
        return best, _route_distance(best)

    best_dist = _route_distance(best)
    improved = True
    it = 0

    while improved and it < max_iter:
        improved = False
        it += 1
        for i in range(1, n - 2):
            for j in range(i + 1, n - 1):
                new_route = best[:]
                new_route[i:j + 1] = reversed(best[i:j + 1])
                new_dist = _route_distance(new_route)
                if new_dist < best_dist:
                    best = new_route
                    best_dist = new_dist
                    improved = True
                    break
            if improved:
                break

    return best, best_dist


def _route_distance(route: list[RouteStop]) -> float:
    d = 0.0
    for i in range(len(route) - 1):
        d += haversine(route[i].lat, route[i].lng, route[i + 1].lat, route[i + 1].lng)
    return d


def _stop_city(stop: RouteStop) -> str:
    """Best-effort city for a stop: its city field, else parsed from address."""
    city = (getattr(stop, "city", "") or "").strip().lower()
    if not city:
        address = getattr(stop, "address", "") or ""
        m = re.search(r",\s*([^,]+?)\s*,\s*(?:TX|TEXAS)\b", address, re.IGNORECASE)
        if m:
            city = m.group(1)
        else:
            # "Frisco TX 75035" without the comma before the state
            m = re.search(r"(?:^|[,]\s*|\s)([A-Za-z][A-Za-z .'-]*?)\s+(?:TX|TEXAS)\s*\d{0,5}\s*$",
                          address.strip(), re.IGNORECASE)
            city = m.group(1) if m else ""
    return re.sub(r"[^a-z ]", "", city.lower()).strip()


def _stop_county(stop: RouteStop) -> Optional[str]:
    """County a stop belongs to: explicit attr wins, else city lookup."""
    explicit = getattr(stop, "county", None)
    if isinstance(explicit, str) and explicit.strip().lower() in COUNTY_COURTHOUSES:
        return explicit.strip().lower()
    return _CITY_TO_COUNTY.get(_stop_city(stop))


def _nearest_county_to_centroid(targets: list[RouteStop]) -> str:
    """County whose courthouse is closest to the target centroid (tie-break)."""
    lat = sum(t.lat for t in targets) / len(targets)
    lng = sum(t.lng for t in targets) / len(targets)
    return min(
        COUNTY_COURTHOUSES,
        key=lambda c: haversine(lat, lng,
                                COUNTY_COURTHOUSES[c][0][0], COUNTY_COURTHOUSES[c][0][1]),
    )


def select_courthouse(targets: list[RouteStop]) -> tuple[float, float, str]:
    """Pick the courthouse serving the county holding most targets (E7).

    County assignment is explicit — via each stop's `county` attribute or an
    exact known-city match — never a latitude guess that sent Grayson targets
    to McKinney. Ties (or wholly unclassified clusters) fall back to whichever
    courthouse is nearest the target centroid. Returns (lat, lng, name); the
    Grayson label flags its coordinates as approximate.
    """
    if not targets:
        raise ValueError("select_courthouse requires at least one target")

    counts = {county: 0 for county in COUNTY_COURTHOUSES}
    for target in targets:
        county = _stop_county(target)
        if county:
            counts[county] += 1

    best = max(counts.values())
    leaders = [c for c, n in counts.items() if n == best] if best > 0 else []
    county = leaders[0] if len(leaders) == 1 else _nearest_county_to_centroid(targets)

    coords, name = COUNTY_COURTHOUSES[county]
    return coords[0], coords[1], name


def optimize_route(targets: list[RouteStop]) -> dict:
    """Optimize a drive-by inspection route. Returns route + stats."""
    if not targets:
        return {"error": "No targets"}

    lat, lng, name = select_courthouse(targets)
    start = RouteStop(title=name, address="Courthouse", city="", lat=lat, lng=lng, category="Start/End")

    raw_route, raw_dist = solve_tsp_nn(start, targets)
    opt_route, opt_dist = optimize_2opt(raw_route)

    # Build schedule with timing
    avg_speed = 30.0  # mph
    inspect_min = 15.0  # per property
    schedule = []
    cum_dist = 0.0
    cum_time = 0.0

    for i, node in enumerate(opt_route):
        if i == 0:
            seg_dist = 0.0
            seg_time = 0.0
        else:
            prev = opt_route[i - 1]
            seg_dist = haversine(prev.lat, prev.lng, node.lat, node.lng)
            seg_time = (seg_dist / avg_speed) * 60.0

        cum_dist += seg_dist
        cum_time += seg_time
        is_inspect = node.category != "Start/End"
        inspect_time = inspect_min if is_inspect else 0.0
        # E6: fold this stop's inspection into cum_time BEFORE reporting, so
        # cumul_min counts each inspection exactly once (elapsed minutes on
        # completion of this stop). The old code added the inspections again
        # via a running sum over the schedule, double-counting every stop.
        cum_time += inspect_time

        schedule.append({
            "seq": i, "title": node.title, "address": node.address,
            "city": node.city, "category": node.category,
            "value": node.value,
            "segment_miles": round(seg_dist, 2),
            "travel_min": round(seg_time, 1),
            "inspect_min": inspect_time,
            "cumul_miles": round(cum_dist, 2),
            "cumul_min": round(cum_time, 1),
        })

    return {
        "courthouse": name,
        "total_miles": round(opt_dist, 2),
        "total_stops": len(targets),
        "schedule": schedule,
    }


if __name__ == "__main__":
    # Self-check: haversine + TSP
    d = haversine(33.1976, -96.6154, 32.7801, -96.8048)
    assert 25 < d < 35, f"DFW courthouse distance should be ~30mi, got {d:.1f}"

    targets = [
        RouteStop("A", "100 Main", "Dallas", 32.78, -96.80, "flip", 300_000),
        RouteStop("B", "200 Oak", "Plano", 33.02, -96.70, "rental", 250_000),
        RouteStop("C", "300 Elm", "Frisco", 33.15, -96.82, "flip", 400_000),
    ]
    result = optimize_route(targets)
    assert result["total_stops"] == 3
    assert result["total_miles"] > 0
    assert len(result["schedule"]) == 5  # 3 targets + start + return
    print(f"route_optimizer.py: {result['total_miles']}mi, {result['total_stops']} stops, courthouse={result['courthouse']}")

    # Self-check (E7): explicit county-aware courthouse selection
    lat, lng, name = select_courthouse([
        RouteStop("G1", "1 Elm St", "Sherman", 33.6357, -96.6089),
        RouteStop("G2", "2 Main St", "Denison", 33.7557, -96.5374),
    ])
    assert "Grayson" in name and "approximate" in name, name
    grayson_coords_ok = abs(lat - GRAYSON_COURTHOUSE[0]) < 1e-9 and abs(lng - GRAYSON_COURTHOUSE[1]) < 1e-9
    assert grayson_coords_ok

    lat, lng, name = select_courthouse(targets)  # Dallas x1 + Plano/Frisco (Collin) x2
    assert name == "Collin County Courthouse", name

    lat, lng, name = select_courthouse([
        RouteStop("D1", "1 Main St", "Dallas", 32.78, -96.80),
        RouteStop("G1", "1 Elm St", "Sherman", 33.64, -96.61),
    ])  # 1-1 tie -> nearest courthouse to centroid decides deterministically
    mid_lat, mid_lng = (32.78 + 33.64) / 2, (-96.80 - 96.61) / 2
    expected = min(COUNTY_COURTHOUSES, key=lambda c: haversine(
        mid_lat, mid_lng, COUNTY_COURTHOUSES[c][0][0], COUNTY_COURTHOUSES[c][0][1]))
    assert name == COUNTY_COURTHOUSES[expected][1], (name, expected)

    # explicit county attribute beats city parsing
    lat, lng, name = select_courthouse([RouteStop("X", "1 Nowhere St", "", 33.0, -96.5)])
    t = RouteStop("Y", "1 Nowhere St", "", 33.0, -96.5)
    t.county = "grayson"
    assert select_courthouse([t])[2].startswith("Grayson County Courthouse")

    # address-parsed city when the city field is blank ("..., City, TX 75000")
    addr_only = RouteStop("P", "1200 W Crawford St, Denison, TX 75020", "",
                          33.7536, -96.5675)
    assert _stop_city(addr_only) == "denison"
    assert _stop_county(addr_only) == "grayson"
    assert select_courthouse([addr_only])[2].startswith("Grayson County Courthouse")

    try:
        select_courthouse([])
        raise AssertionError("empty targets should fail loud")
    except ValueError:
        pass
    print("route_optimizer.py: E7 courthouse selection self-check passed")

    # Self-check (E6): cumul_min counts each inspection exactly once.
    schedule = result["schedule"]
    for i in range(1, len(schedule)):
        expected_prev = (schedule[i - 1]["cumul_min"]
                         + schedule[i]["travel_min"] + schedule[i]["inspect_min"])
        assert abs(schedule[i]["cumul_min"] - expected_prev) <= 0.11, (
            f"row {i}: {schedule[i]['cumul_min']} != {expected_prev}")
    travel_total = sum(s["travel_min"] for s in schedule)
    inspect_total = sum(s["inspect_min"] for s in schedule)
    final = schedule[-1]["cumul_min"]
    assert abs(final - (travel_total + inspect_total)) < 0.3, (final, travel_total, inspect_total)
    # old bug would have reported roughly travel + 2*inspect here:
    assert final < travel_total + inspect_total + 0.3
    print(f"route_optimizer.py: E6 cumul_min self-check passed "
          f"(final={final}min = {travel_total:.1f} travel + {inspect_total:.0f} inspect)")

    print("route_optimizer.py: all self-checks passed")