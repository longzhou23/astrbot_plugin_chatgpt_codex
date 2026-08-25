import json
import tempfile
import unittest
from pathlib import Path

from ..model_catalog import ModelCatalog, parse_models


class ModelCatalogTests(unittest.TestCase):
    def test_parse_server_shape_and_preserve_effort_order(self):
        models = parse_models(
            {
                "models": [
                    {
                        "id": "gpt-a",
                        "displayName": "A",
                        "supportedReasoningEfforts": ["low", "high"],
                    },
                    {"id": "gpt-hidden", "hidden": True},
                    {"id": "gpt-a"},
                ]
            }
        )
        self.assertEqual([model.id for model in models], ["gpt-a", "gpt-hidden"])
        self.assertEqual(models[0].reasoning_efforts, ("low", "high"))

    def test_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            catalog = ModelCatalog(path)
            models = parse_models({"data": [{"id": "gpt-a", "reasoningEfforts": ["medium"]}]})
            catalog.replace(models)
            loaded = ModelCatalog(path)
            self.assertEqual(loaded.models[0].id, "gpt-a")
            self.assertEqual(json.loads(path.read_text())["models"][0]["id"], "gpt-a")
