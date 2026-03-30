import unittest
from pathlib import Path

from app.ingest import (
    _market_bbox,
    _normalize_address_features,
    _normalize_building_features,
    _normalize_parcel_features,
    _normalize_place_features,
    _normalize_redevelopment_features,
    _normalize_transit_features,
    load_redevelopment_areas,
    load_transit_stops,
    load_newark_boundary,
    load_newark_parcels,
    load_overture_features,
)


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "newark"


class IngestFixtureTests(unittest.TestCase):
    def test_boundary_fixture_and_bbox(self):
        boundary = load_newark_boundary(Path("."), Path("."), fixtures_dir=FIXTURES_DIR)
        bbox = _market_bbox(boundary)
        self.assertEqual(boundary["geometry"]["type"], "Polygon")
        self.assertEqual(len(bbox), 4)

    def test_parcel_fixture_normalization(self):
        parcels = _normalize_parcel_features(load_newark_parcels(Path("."), fixtures_dir=FIXTURES_DIR))
        self.assertEqual(len(parcels), 2)
        self.assertEqual(parcels[0]["property_class_code"], "4A")

    def test_overture_fixture_normalization(self):
        buildings = _normalize_building_features(
            load_overture_features(Path("."), [-74.19, 40.73, -74.18, 40.74], "building", fixtures_dir=FIXTURES_DIR, overture_cli="overturemaps")
        )
        addresses = _normalize_address_features(
            load_overture_features(Path("."), [-74.19, 40.73, -74.18, 40.74], "address", fixtures_dir=FIXTURES_DIR, overture_cli="overturemaps")
        )
        places = _normalize_place_features(
            load_overture_features(Path("."), [-74.19, 40.73, -74.18, 40.74], "place", fixtures_dir=FIXTURES_DIR, overture_cli="overturemaps")
        )
        self.assertEqual(len(buildings), 2)
        self.assertEqual(len(addresses), 2)
        self.assertEqual(len(places), 3)
        self.assertTrue(addresses[0]["display_name"])

    def test_transit_and_redevelopment_fixture_normalization(self):
        redevelopment_areas = _normalize_redevelopment_features(
            load_redevelopment_areas(
                Path("."),
                [-74.19, 40.73, -74.18, 40.74],
                fixtures_dir=FIXTURES_DIR,
                redevelopment_url="https://example.com/redevelopment",
            )
        )
        transit_stops = _normalize_transit_features(
            load_transit_stops(
                Path("."),
                [-74.19, 40.73, -74.18, 40.74],
                fixtures_dir=FIXTURES_DIR,
                light_rail_url="https://example.com/light-rail",
                nj_transit_station_url="https://example.com/nj-transit",
                path_station_url="https://example.com/path",
            )
        )
        self.assertEqual(len(redevelopment_areas), 1)
        self.assertEqual(redevelopment_areas[0]["short_name"], "Broad Street")
        self.assertEqual(len(transit_stops), 2)
        self.assertEqual(transit_stops[0]["stop_type"], "Light Rail")
