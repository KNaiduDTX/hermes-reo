# src/adapters/route_optimizer.py
"""TSP route optimizer for drive-by property inspections — PORTED from lucid-noether.

Nearest-neighbor heuristic + 2-opt improvement. Used for auction prep
(drive-by only, no interior access).
"""
from __future__ import annotations
import math
import json
from dataclasses import dataclass
from typing import Optional

COLLIN_COURTHOUSE = (33.1976, -96.6154)   # 2100 Bloomdale Rd, McKinney
DALLAS_COURTHOUSE = (32.7801, -96.8048)   # George Allen Courts Building
GRAYSON_COURTHOUSE = (33.6407, -96.6061)  # Sherman


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


def select_courthouse(targets: list[RouteStop]) -> tuple[float, float, str]:
    """Pick the courthouse closest to the cluster of targets."""
    dallas_count = sum(1 for t in targets if t.lat < 33.0)
    collin_count = len(targets) - dallas_count
    if dallas_count > collin_count:
        return DALLAS_COURTHOUSE[0], DALLAS_COURTHOUSE[1], "Dallas County Courthouse"
    return COLLIN_COURTHOUSE[0], COLLIN_COURTHOUSE[1], "Collin County Courthouse"


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

        schedule.append({
            "seq": i, "title": node.title, "address": node.address,
            "city": node.city, "category": node.category,
            "value": node.value,
            "segment_miles": round(seg_dist, 2),
            "travel_min": round(seg_time, 1),
            "inspect_min": inspect_time,
            "cumul_miles": round(cum_dist, 2),
            "cumul_min": round(cum_time + (sum(s["inspect_min"] for s in schedule)), 1),
        })
        if is_inspect:
            cum_time += inspect_min

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
    print("route_optimizer.py: all self-checks passed")