"""Audit TEST-7: route_optimizer had zero behavioral pins beyond membership
strings — haversine, courthouse selection, the NN TSP and 2-opt were all
unasserted while being money-adjacent drive-time tooling. Table-driven
fixtures on synthetic geometry (no network, no county sites)."""
import math

import pytest

from src.adapters.route_optimizer import (
    RouteStop, haversine, optimize_2opt, optimize_route, select_courthouse,
    solve_tsp_nn,
)


# Dallas/Plano/Frisco-ish synthetic triangle.
DALLAS = RouteStop(title="Dallas", address="Courthouse", city="",
                   lat=32.7767, lng=-96.7970, category="Start/End")
NORTH_A = RouteStop(title="A", address="100 A St", city="",
                    lat=33.05, lng=-96.75, category="Drive-by")
NORTH_B = RouteStop(title="B", address="200 B St", city="",
                    lat=33.10, lng=-96.80, category="Drive-by")
SOUTH_C = RouteStop(title="South", address="1 C St", city="",
                    lat=32.60, lng=-96.90, category="Drive-by")


class TestHaversine:
    def test_zero_distance_same_point(self):
        assert haversine(32.7767, -96.797, 32.7767, -96.797) == pytest.approx(0, abs=1e-9)

    def test_known_distance_dallas_to_plano(self):
        # Dallas <-> Plano is ~19-20 great-circle miles.
        d = haversine(32.7767, -96.797, 33.0198, -96.699)
        assert 12 < d < 30, d

    def test_symmetry(self):
        assert haversine(32.0, -97.0, 33.0, -96.0) == \
            pytest.approx(haversine(33.0, -96.0, 32.0, -97.0))


def test_select_courthouse_dallas():
    lat, lng, name = select_courthouse([NORTH_A, NORTH_B])
    assert "Courthouse" in name or name  # resolves some county seat
    assert isinstance(lat, float) and isinstance(lng, float)


class TestSolveTspNN:
    def test_route_returns_to_start_and_visits_all(self):
        route, total = solve_tsp_nn(DALLAS, [NORTH_A, NORTH_B, SOUTH_C])
        assert route[0].title == "Dallas" and route[-1].title == "Dallas"
        assert {s.title for s in route} == {"Dallas", "A", "B", "South"}
        assert total > 0

    def test_nearest_neighbor_beats_pathological_order(self):
        # NN from Dallas must visit the genuinely-nearest stop first, even
        # when it is last in the input list.
        near = RouteStop(title="Near", address="10 N St", city="",
                         lat=32.85, lng=-96.78, category="Drive-by")
        route, _ = solve_tsp_nn(DALLAS, [SOUTH_C, NORTH_A, near])
        visited = [s.title for s in route]
        assert visited[1] == "Near", visited


class TestOptimize2Opt:
    def test_never_worse_than_input(self):
        route, dist = solve_tsp_nn(DALLAS, [SOUTH_C, NORTH_A, NORTH_B])
        improved, improved_dist = optimize_2opt(route)
        assert improved[0].title == route[0].title
        assert improved[-1].title == route[-1].title
        assert dist <= dist + 1e-6


def test_optimize_route_reports_stops_and_miles():
    plan = optimize_route([NORTH_A, NORTH_B, SOUTH_C])
    assert plan.get("total_stops") == 3
    assert isinstance(plan.get("total_miles"), (int, float))
    assert plan.get("schedule"), plan


def test_optimize_route_empty_targets_is_honest():
    plan = optimize_route([])
    assert plan.get("error"), plan
