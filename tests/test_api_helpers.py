import unittest

from app.geojson import feature_collection, row_to_feature
from app.repository import parse_bbox


class ApiHelperTests(unittest.TestCase):
    def test_parse_bbox(self):
        self.assertEqual(parse_bbox("-74.2,40.7,-74.1,40.8"), (-74.2, 40.7, -74.1, 40.8))
        with self.assertRaises(ValueError):
            parse_bbox("-74.2,40.7,-74.1")
        with self.assertRaises(ValueError):
            parse_bbox("-74.1,40.8,-74.2,40.7")

    def test_geojson_serialization(self):
        row = {
            "id": "parcel-1",
            "classification": "COMMERCIAL",
            "priority_score": 88.0,
            "geometry_json": '{"type":"Polygon","coordinates":[[[-74.2,40.7],[-74.1,40.7],[-74.1,40.8],[-74.2,40.8],[-74.2,40.7]]]}',
        }
        feature = row_to_feature(row)
        collection = feature_collection([feature])
        self.assertEqual(collection["type"], "FeatureCollection")
        self.assertEqual(collection["features"][0]["properties"]["id"], "parcel-1")
