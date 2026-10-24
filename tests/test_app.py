import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

BASE = "https://example.test/bench/"

LOGIC_CSV = """\ufeff"模型","极限分数","中位分数","Think"
"Model A","80.00","70.00","1"
"Model B","70.00","60.00","1"
"""

CODE_CSV = """Model,Web(E),Think
Model A,Pass,1
Model D,4/A+(1.0),1
"""


def catalog(*entries):
    return json.dumps({"datasets": list(entries)}, ensure_ascii=False).encode("utf-8")


def dataset_entry(category="logic", report_date="2026-10", csv_path="data/logic/2026-10.csv", title="月榜"):
    return {
        "category": category,
        "reportDate": report_date,
        "tableIndex": 0,
        "title": title,
        "csv": csv_path,
    }


class FakeFetcher:
    def __init__(self, files):
        self.files = {
            key: value.encode("utf-8") if isinstance(value, str) else value
            for key, value in files.items()
        }

    def __call__(self, url, etag=None, last_modified=None):
        path = urlsplit(url).path
        key = path.split("/bench/", 1)[1] if "/bench/" in path else path.lstrip("/")
        if key not in self.files:
            raise app.FetchError(f"missing {key}")
        body = self.files[key]
        tag = '"' + hashlib.sha256(body).hexdigest() + '"'
        if etag == tag:
            return app.FetchResult(304, None, tag, None)
        return app.FetchResult(200, body, tag, None)


class FailingCatalogFetcher(FakeFetcher):
    def __call__(self, url, etag=None, last_modified=None):
        if url.endswith("datasets.json"):
            raise app.FetchError("catalog down")
        return super().__call__(url, etag, last_modified)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        app.init_db(self.db_path)
        self.conn = app.open_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def poll(self, files, fetcher_class=FakeFetcher):
        fetcher = fetcher_class(files)
        app.poll_once(self.conn, fetcher, BASE)
        return fetcher

    def baseline(self):
        files = {
            "data/datasets.json": catalog(dataset_entry()),
            "data/logic/2026-10.csv": LOGIC_CSV,
        }
        self.poll(files)
        self.assertEqual(app.get_meta(self.conn, "last_poll_status"), "ok")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        return files

    def test_baseline_is_silent_and_stores_snapshot(self):
        files = self.baseline()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM models").fetchone()[0], 2)
        self.assertIsNotNone(app.get_meta(self.conn, "baseline_at"))
        self.assertEqual(app.get_meta(self.conn, "last_poll_status"), "ok")
        self.assertEqual(files["data/datasets.json"], catalog(dataset_entry()))

    def test_unchanged_poll_writes_no_events(self):
        self.baseline()
        old_poll_at = app.get_meta(self.conn, "last_poll_at")
        files = {
            "data/datasets.json": catalog(dataset_entry()),
            "data/logic/2026-10.csv": LOGIC_CSV,
        }
        self.poll(files)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        self.assertEqual(app.get_meta(self.conn, "last_poll_status"), "ok")
        self.assertGreaterEqual(app.get_meta(self.conn, "last_poll_at"), old_poll_at)

    def test_existing_dataset_emits_score_add_remove(self):
        self.baseline()
        changed_csv = """"模型","极限分数","中位分数","Think"
"Model A","81.00","70.50","1"
"Model C","75.00","65.00","1"
"""
        files = {
            "data/datasets.json": catalog(dataset_entry()),
            "data/logic/2026-10.csv": changed_csv,
        }
        self.poll(files)
        rows = self.conn.execute("SELECT type, model, details FROM events ORDER BY id").fetchall()
        self.assertEqual([row["type"] for row in rows], ["score_changed", "model_added", "model_removed"])
        changed = json.loads(rows[0]["details"])
        self.assertEqual(rows[0]["model"], "Model A")
        self.assertEqual({item["field"] for item in changed["changes"]}, {"极限分数", "中位分数"})
        self.assertEqual(rows[1]["model"], "Model C")
        self.assertEqual(rows[2]["model"], "Model B")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 2)

    def test_new_dataset_announces_only_globally_new_models(self):
        self.baseline()
        files = {
            "data/datasets.json": catalog(
                dataset_entry(),
                dataset_entry("code_v3", "2026-09", "data/code_v3/2026-09.csv"),
            ),
            "data/logic/2026-10.csv": LOGIC_CSV,
            "data/code_v3/2026-09.csv": CODE_CSV,
        }
        self.poll(files)
        rows = self.conn.execute("SELECT type, model, dataset_label, details FROM events ORDER BY id").fetchall()
        self.assertEqual([row["type"] for row in rows], ["dataset_added", "model_added"])
        dataset_event = rows[0]
        self.assertEqual(dataset_event["dataset_label"], "Agentic · 2026-09 · 月榜")
        self.assertEqual(json.loads(dataset_event["details"])["row_count"], 2)
        self.assertEqual(rows[1]["model"], "Model D")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 4
        )

    def test_invalid_csv_keeps_previous_snapshot(self):
        self.baseline()
        files = {
            "data/datasets.json": catalog(dataset_entry()),
            "data/logic/2026-10.csv": "<html>upstream error</html>",
        }
        self.poll(files)
        self.assertEqual(app.get_meta(self.conn, "last_poll_status"), "error")
        self.assertIn("HTML", app.get_meta(self.conn, "last_error"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        content_hash = self.conn.execute("SELECT content_hash FROM datasets").fetchone()[0]
        self.assertIsNotNone(content_hash)

    def test_catalog_failure_keeps_previous_snapshot(self):
        self.baseline()
        files = {
            "data/datasets.json": catalog(dataset_entry()),
            "data/logic/2026-10.csv": LOGIC_CSV,
        }
        self.poll(files, FailingCatalogFetcher)
        self.assertEqual(app.get_meta(self.conn, "last_poll_status"), "error")
        self.assertIn("catalog", app.get_meta(self.conn, "last_error"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_parse_csv_with_report_date_bom_and_think_variants(self):
        text = """\ufeff报告日期,模型,极限分数,中位分数,Think
2026-05,GLM-5.1,65.51,55.59,1
2026-05,GLM-5.1,23.88,19.07,0
"""
        headers, fields, rows = app.parse_csv(text, "logic")
        self.assertEqual(headers[1], "模型")
        self.assertEqual(fields, ["极限分数", "中位分数"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["model"], "GLM-5.1")
        self.assertEqual(rows[0]["think"], "1")
        self.assertEqual(rows[1]["think"], "0")
        self.assertEqual(rows[0]["scores"]["中位分数"], "55.59")

    def test_parse_catalog_filters_hidden_and_absolute_paths(self):
        payload = catalog(
            dataset_entry("code", "2026-02", "data/code/2026-02.csv"),
            dataset_entry("vision", "2025-11", "data/vision/2025-11.csv"),
            dataset_entry(),
            dataset_entry("logic", "2026-09", "https://example.test/evil.csv"),
        )
        specs = app.parse_catalog(payload)
        self.assertEqual([spec["key"] for spec in specs], ["data/logic/2026-10.csv"])
        self.assertEqual(specs[0]["label"], "推理 · 2026-10 · 月榜")
        with self.assertRaises(app.DataError):
            app.parse_catalog(catalog(dataset_entry("code", "2026-02", "data/code/2026-02.csv")))

    def test_score_columns_per_category(self):
        logic_headers = ["模型", "极限分数", "中位分数", "Think"]
        self.assertEqual(app.score_columns("logic", logic_headers), ["极限分数", "中位分数"])
        code_headers = ["Model", "MacOS App(C)", "Web(E)", "Unprompted", "Scaffold", "Think"]
        self.assertEqual(app.score_columns("code_v3", code_headers), ["MacOS App(C)", "Web(E)"])
        fallback_headers = ["模型", "多轮总分", "测试成本(元)"]
        self.assertEqual(app.score_columns("unknown", fallback_headers), ["多轮总分"])

    def test_api_events_and_leaderboard(self):
        self.baseline()
        changed_csv = """"模型","极限分数","中位分数","Think"
"Model A","81.00","70.50","1"
"Model B","70.00","60.01","1"
"""
        files = {
            "data/datasets.json": catalog(dataset_entry()),
            "data/logic/2026-10.csv": changed_csv,
        }
        self.poll(files)
        events = app.api_events(self.conn, {"type": ["score_changed"]})
        self.assertEqual(events["total"], 2)
        self.assertTrue(all(item["type"] == "score_changed" for item in events["items"]))
        with self.assertRaises(app.BadRequest):
            app.api_events(self.conn, {"type": ["nope"]})
        with self.assertRaises(app.BadRequest):
            app.api_events(self.conn, {"category": ["vision"]})
        board = app.api_leaderboard(self.conn, {"category": ["logic"]})
        self.assertEqual(board["category"], "logic")
        self.assertEqual(board["dataset"]["key"], "data/logic/2026-10.csv")
        self.assertEqual(len(board["rows"]), 2)
        self.assertEqual(board["score_fields"], ["极限分数", "中位分数"])
        fallback = app.api_leaderboard(self.conn, {"category": ["missing"]})
        self.assertEqual(fallback["category"], "logic")

    def test_same_value(self):
        self.assertTrue(app.same_value("80.0", "80.00"))
        self.assertFalse(app.same_value("4/A+(341.72)", "4/A+(341.720)"))
        self.assertFalse(app.same_value("80", "81"))
        self.assertTrue(app.same_value("", ""))


if __name__ == "__main__":
    unittest.main()
