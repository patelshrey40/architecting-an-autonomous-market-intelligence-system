import json


def feature_collection(features):
    return {"type": "FeatureCollection", "features": list(features)}


def row_to_feature(row):
    geometry = json.loads(row["geometry_json"]) if row["geometry_json"] else None
    properties = {
        key: value
        for key, value in row.items()
        if key != "geometry_json"
    }
    return {"type": "Feature", "geometry": geometry, "properties": properties}
