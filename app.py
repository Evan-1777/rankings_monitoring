#!/usr/bin/env python3
"""Monitor the Zhizhi LLM benchmark for new models and score changes.

The site publishes a dataset catalog and one CSV per leaderboard. This service
polls both, stores the latest rows in SQLite, and turns differences into events.
Only the Python standard library is used so the container stays small.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import os
import signal
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import Request, urlopen

log = logging.getLogger("rankings")


def _int_env(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise SystemExit(f"{name} must be >= {minimum}, got {value}")
    return value


SOURCE_URL = os.environ.get(
    "SOURCE_URL", "https://llm2014.github.io/llm_benchmark/"
).strip() or "https://llm2014.github.io/llm_benchmark/"
BASE_URL = SOURCE_URL.rstrip("/") + "/"
POLL_INTERVAL = _int_env("POLL_INTERVAL", 900, 60)
REQUEST_TIMEOUT = _int_env("REQUEST_TIMEOUT", 20, 5)
PORT = _int_env("PORT", 8080, 1)
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DB_PATH = DATA_DIR / "rankings.db"
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BYTES = 5_000_000
USER_AGENT = "rankings-monitoring/1.0 (+https://github.com/Evan-1777/rankings_monitoring)"

MONITORED_CATEGORIES = ("logic", "code_v3")
CATEGORY_LABELS = {"logic": "推理", "code_v3": "Agentic"}
CATEGORY_ORDER = list(MONITORED_CATEGORIES)
SCORE_COLUMNS = {"logic": ("极限分数", "中位分数")}
EVENT_TYPES = ("dataset_added", "model_added", "score_changed", "model_removed")


SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    key TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    report_date TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    csv_path TEXT NOT NULL,
    label TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    score_fields TEXT NOT NULL DEFAULT '[]',
    content_hash TEXT,
    etag TEXT,
    last_modified TEXT,
    first_seen TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS entries (
    dataset_key TEXT NOT NULL REFERENCES datasets(key) ON DELETE CASCADE,
    model TEXT NOT NULL,
    think TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL,
    data TEXT NOT NULL,
    scores TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dataset_key, model, think)
);

CREATE TABLE IF NOT EXISTS models (
    model TEXT NOT NULL,
    think TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (model, think)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    category TEXT NOT NULL,
    dataset_key TEXT NOT NULL,
    dataset_label TEXT NOT NULL,
    model TEXT,
    think TEXT,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);
CREATE INDEX IF NOT EXISTS idx_entries_dataset ON entries(dataset_key, position);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DataError(Exception):
    """Raised when upstream content cannot be safely applied."""


class FetchError(Exception):
    """Raised when an upstream request fails."""


class BadRequest(Exception):
    """Raised for invalid API parameters."""


@dataclass(frozen=True)
class FetchResult:
    status: int
    body: bytes | None = None
    etag: str | None = None
    last_modified: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def open_db(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(path: Path | None = None) -> None:
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(target)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute(
            "UPDATE datasets SET active = 0 WHERE category NOT IN (?, ?)",
            MONITORED_CATEGORIES,
        )
        conn.commit()
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def meta_map(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category)


def display_model(model: str | None, think: str | None) -> str | None:
    if not model:
        return None
    return f"{model}（非思考）" if think == "0" else model


def parse_catalog(payload: bytes) -> list[dict]:
    """Return visible leaderboard specs from the site catalog.

    The catalog is cumulative and also carries a hidden legacy category and a
    vision category outside this monitor's scope. Only logic and code_v3 are
    imported.
    """
    try:
        doc = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataError(f"invalid catalog: {exc}") from exc
    entries = doc.get("datasets") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        raise DataError("invalid catalog: missing datasets list")

    specs: dict[str, dict] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip()
        csv_path = str(item.get("csv", "")).strip()
        if not category or not csv_path or category not in MONITORED_CATEGORIES:
            continue
        split = urlsplit(csv_path)
        if split.scheme or split.netloc or csv_path.startswith("/"):
            continue
        report_date = str(item.get("reportDate", "")).strip()
        title = str(item.get("title", "")).strip()
        label = " · ".join(
            part for part in (category_label(category), report_date, title) if part
        )
        specs[csv_path] = {
            "key": csv_path,
            "category": category,
            "report_date": report_date,
            "title": title,
            "csv_path": csv_path,
            "label": label,
        }
    if not specs:
        raise DataError("catalog contains no monitored datasets")
    return list(specs.values())


def score_columns(category: str, headers: list[str]) -> list[str]:
    fixed = SCORE_COLUMNS.get(category)
    if fixed:
        return [header for header in fixed if header in headers]
    if category == "code_v3":
        metadata = {"model", "unprompted", "scaffold", "think", "报告日期"}
        return [header for header in headers if header and header.casefold() not in metadata]
    return [
        header
        for header in headers
        if header and ("分数" in header or "总分" in header or "score" in header.casefold())
    ]


def parse_csv(text: str, category: str) -> tuple[list[str], list[str], list[dict]]:
    """Parse one leaderboard CSV into normalized rows."""
    if not text.strip():
        raise DataError("empty CSV")
    if text.lstrip().startswith("<"):
        raise DataError("response is HTML, not CSV")

    try:
        reader = csv.reader(io.StringIO(text))
        raw_headers = next(reader)
    except StopIteration as exc:
        raise DataError("empty CSV") from exc
    except csv.Error as exc:
        raise DataError(f"invalid CSV: {exc}") from exc

    headers = [header.strip().lstrip("\ufeff") for header in raw_headers]
    model_index = next(
        (
            index
            for index, header in enumerate(headers)
            if header.casefold() in {"模型", "model"}
        ),
        None,
    )
    if model_index is None:
        raise DataError("missing model column")

    think_index = next(
        (index for index, header in enumerate(headers) if header.casefold() == "think"),
        None,
    )
    fields = score_columns(category, headers)
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()

    try:
        for raw in reader:
            cells = [cell.strip() for cell in raw]
            if not any(cells):
                continue
            if len(cells) <= model_index or not cells[model_index]:
                continue
            model = cells[model_index]
            data = {
                header: (cells[index] if index < len(cells) else "")
                for index, header in enumerate(headers)
                if header
            }
            think = (
                cells[think_index]
                if think_index is not None and think_index < len(cells)
                else ""
            )
            key = (model, think)
            if key in seen:
                log.warning("duplicate row %s / think=%r in %s", model, think, category)
                continue
            seen.add(key)
            rows.append(
                {
                    "model": model,
                    "think": think,
                    "position": len(rows) + 1,
                    "data": data,
                    "scores": {field: data.get(field, "") for field in fields},
                }
            )
    except csv.Error as exc:
        raise DataError(f"invalid CSV: {exc}") from exc

    if not rows:
        raise DataError("CSV contains no data rows")
    return headers, fields, rows


def fetch_http(
    url: str, etag: str | None = None, last_modified: str | None = None
) -> FetchResult:
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise FetchError(f"response exceeds {MAX_BYTES} bytes")
            if response.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return FetchResult(
                status=200,
                body=body,
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
    except HTTPError as exc:
        if exc.code == 304:
            return FetchResult(status=304, etag=etag, last_modified=last_modified)
        raise FetchError(f"HTTP {exc.code} for {url}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise FetchError(f"{reason} for {url}") from exc


def same_value(left: str, right: str) -> bool:
    if left == right:
        return True
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return False


def load_entry_map(conn: sqlite3.Connection, dataset_key: str) -> dict[tuple[str, str], dict]:
    entries: dict[tuple[str, str], dict] = {}
    for row in conn.execute(
        "SELECT model, think, position, scores FROM entries WHERE dataset_key = ?",
        (dataset_key,),
    ):
        try:
            scores = json.loads(row["scores"])
        except json.JSONDecodeError:
            scores = {}
        entries[(row["model"], row["think"])] = {
            "position": row["position"],
            "scores": scores,
        }
    return entries


def dataset_events(
    spec: dict,
    old_entries: dict[tuple[str, str], dict],
    rows: list[dict],
    score_fields: list[str],
    is_new: bool,
    is_baseline: bool,
    known_models: set[tuple[str, str]],
) -> list[tuple[str, str | None, str, dict]]:
    """Build events for one freshly parsed dataset.

    A new month normally re-lists models that are already known. It therefore
    gets one dataset event, plus model events only for globally new models;
    additions to an existing leaderboard are announced per model.
    """
    if is_baseline:
        return []

    events: list[tuple[str, str | None, str, dict]] = []
    new_keys = {(row["model"], row["think"]) for row in rows}

    if is_new:
        events.append(
            (
                "dataset_added",
                None,
                "",
                {
                    "report_date": spec["report_date"],
                    "row_count": len(rows),
                    "score_fields": score_fields,
                },
            )
        )
        for row in rows:
            key = (row["model"], row["think"])
            if key not in known_models:
                events.append(
                    (
                        "model_added",
                        row["model"],
                        row["think"],
                        {"position": row["position"], "scores": row["scores"]},
                    )
                )
        return events

    for row in rows:
        key = (row["model"], row["think"])
        if key not in old_entries:
            events.append(
                (
                    "model_added",
                    row["model"],
                    row["think"],
                    {"position": row["position"], "scores": row["scores"]},
                )
            )
            continue
        changes = []
        old_scores = old_entries[key]["scores"]
        for field in score_fields:
            old_value = old_scores.get(field, "")
            new_value = row["scores"].get(field, "")
            if not same_value(old_value, new_value):
                changes.append({"field": field, "old": old_value, "new": new_value})
        if changes:
            events.append(
                (
                    "score_changed",
                    row["model"],
                    row["think"],
                    {"position": row["position"], "changes": changes},
                )
            )

    for model, think in old_entries.keys() - new_keys:
        old = old_entries[(model, think)]
        events.append(
            (
                "model_removed",
                model,
                think,
                {"position": old["position"], "scores": old["scores"]},
            )
        )
    return events


def _write_event(
    conn: sqlite3.Connection,
    event_type: str,
    spec: dict,
    model: str | None,
    think: str,
    details: dict,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO events(type, category, dataset_key, dataset_label, model, think, details, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_type,
            spec["category"],
            spec["key"],
            spec["label"],
            model,
            think or None,
            json.dumps(details, ensure_ascii=False),
            created_at,
        ),
    )


def _upsert_dataset(conn: sqlite3.Connection, spec: dict, row: dict, digest: str, result: FetchResult) -> None:
    conn.execute(
        """
        INSERT INTO datasets(
            key, category, report_date, title, csv_path, label, row_count,
            score_fields, content_hash, etag, last_modified, first_seen,
            checked_at, active
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(key) DO UPDATE SET
            category = excluded.category,
            report_date = excluded.report_date,
            title = excluded.title,
            csv_path = excluded.csv_path,
            label = excluded.label,
            row_count = excluded.row_count,
            score_fields = excluded.score_fields,
            content_hash = excluded.content_hash,
            etag = excluded.etag,
            last_modified = excluded.last_modified,
            checked_at = excluded.checked_at,
            active = 1
        """,
        (
            spec["key"],
            spec["category"],
            spec["report_date"],
            spec["title"],
            spec["csv_path"],
            spec["label"],
            len(row["rows"]),
            json.dumps(row["score_fields"], ensure_ascii=False),
            digest,
            result.etag,
            result.last_modified,
            row["now"],
            row["now"],
        ),
    )


def poll_once(
    conn: sqlite3.Connection,
    fetcher=fetch_http,
    base_url: str = BASE_URL,
) -> None:
    """Fetch the catalog and all active datasets, then apply one consistent update."""
    started = time.monotonic()
    started_at = utc_now()
    started_iso = iso(started_at)
    set_meta(conn, "last_poll_status", "running")
    set_meta(conn, "poll_started_at", started_iso)
    conn.commit()

    try:
        catalog_url = urljoin(base_url, "data/datasets.json")
        catalog_result = fetcher(
            catalog_url,
            get_meta(conn, "catalog_etag"),
            get_meta(conn, "catalog_last_modified"),
        )
        if catalog_result.status == 304:
            active = conn.execute(
                "SELECT key, category, report_date, title, csv_path, label "
                "FROM datasets WHERE active = 1 AND category IN (?, ?) "
                "ORDER BY first_seen, key",
                MONITORED_CATEGORIES,
            ).fetchall()
            if not active:
                catalog_result = fetcher(catalog_url)
                if catalog_result.status == 304:
                    raise FetchError("catalog returned 304 without cached datasets")
            else:
                specs = [dict(row) for row in active]
        if catalog_result.status not in (200, 304):
            raise FetchError(f"catalog returned HTTP {catalog_result.status}")
        if catalog_result.status == 200:
            if not catalog_result.body:
                raise DataError("empty catalog response")
            specs = parse_catalog(catalog_result.body)
        catalog_changed = catalog_result.status == 200
    except Exception as exc:
        log.warning("catalog refresh failed: %s", exc)
        _record_failure(conn, started, f"catalog: {exc}")
        return

    existing_rows = {row["key"]: dict(row) for row in conn.execute("SELECT * FROM datasets")}
    new_keys = {spec["key"] for spec in specs} - set(existing_rows)
    is_baseline = get_meta(conn, "baseline_at") is None
    known_models = {
        (row["model"], row["think"])
        for row in conn.execute("SELECT model, think FROM models")
    }

    jobs = []
    for spec in specs:
        old = existing_rows.get(spec["key"])
        jobs.append((spec, old))

    results: list[tuple[dict, dict | None, FetchResult | None, str | None]] = []
    if jobs:
        with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as executor:
            futures = [
                executor.submit(
                    _fetch_dataset, fetcher, base_url, spec, old
                )
                for spec, old in jobs
            ]
            for future in futures:
                results.append(future.result())

    prepared: list[tuple[dict, dict | None, FetchResult, list[str], list[dict], str]] = []
    fetch_errors: list[str] = []
    new_failed = False

    for spec, old, result, error in results:
        if error or result is None:
            fetch_errors.append(error or f"{spec['label']}: request failed")
            if spec["key"] in new_keys:
                new_failed = True
            continue
        if result.status == 304:
            prepared.append((spec, old, result, [], [], ""))
            continue
        if result.status != 200 or result.body is None:
            fetch_errors.append(f"{spec['label']}: HTTP {result.status}")
            if spec["key"] in new_keys:
                new_failed = True
            continue
        digest = hashlib.sha256(result.body).hexdigest()
        if old and old.get("content_hash") == digest:
            prepared.append((spec, old, result, [], [], digest))
            continue
        try:
            text = result.body.decode("utf-8-sig")
            _, score_fields, rows = parse_csv(text, spec["category"])
        except Exception as exc:
            fetch_errors.append(f"{spec['label']}: {exc}")
            if spec["key"] in new_keys:
                new_failed = True
            continue
        prepared.append((spec, old, result, score_fields, rows, digest))

    now = iso(utc_now())
    imported = 0
    checked = 0
    try:
        with conn:
            if catalog_changed:
                conn.execute("UPDATE datasets SET active = 0")
                for spec in specs:
                    if spec["key"] in existing_rows:
                        conn.execute(
                            "UPDATE datasets SET category = ?, report_date = ?, title = ?, "
                            "csv_path = ?, label = ?, active = 1 WHERE key = ?",
                            (
                                spec["category"],
                                spec["report_date"],
                                spec["title"],
                                spec["csv_path"],
                                spec["label"],
                                spec["key"],
                            ),
                        )
                if not new_failed:
                    set_meta(conn, "catalog_etag", catalog_result.etag or "")
                    set_meta(conn, "catalog_last_modified", catalog_result.last_modified or "")

            for spec, old, result, score_fields, rows, digest in prepared:
                if result.status == 304:
                    conn.execute(
                        "UPDATE datasets SET checked_at = ?, "
                        "etag = COALESCE(?, etag), last_modified = COALESCE(?, last_modified) "
                        "WHERE key = ?",
                        (now, result.etag, result.last_modified, spec["key"]),
                    )
                    checked += 1
                    continue
                if not rows:
                    # Content was unchanged; only refresh conditional headers.
                    conn.execute(
                        "UPDATE datasets SET checked_at = ?, "
                        "etag = COALESCE(?, etag), last_modified = COALESCE(?, last_modified) "
                        "WHERE key = ?",
                        (now, result.etag, result.last_modified, spec["key"]),
                    )
                    checked += 1
                    continue

                is_new = old is None
                old_entries = load_entry_map(conn, spec["key"]) if not is_new else {}
                events = dataset_events(
                    spec,
                    old_entries,
                    rows,
                    score_fields,
                    is_new,
                    is_baseline,
                    known_models,
                )
                _upsert_dataset(
                    conn,
                    spec,
                    {"rows": rows, "score_fields": score_fields, "now": now},
                    digest,
                    result,
                )
                conn.execute("DELETE FROM entries WHERE dataset_key = ?", (spec["key"],))
                conn.executemany(
                    "INSERT INTO entries(dataset_key, model, think, position, data, scores, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            spec["key"],
                            row["model"],
                            row["think"],
                            row["position"],
                            json.dumps(row["data"], ensure_ascii=False),
                            json.dumps(row["scores"], ensure_ascii=False),
                            now,
                        )
                        for row in rows
                    ],
                )
                conn.executemany(
                    "INSERT INTO models(model, think, first_seen, last_seen) VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(model, think) DO UPDATE SET last_seen = excluded.last_seen",
                    [(row["model"], row["think"], now, now) for row in rows],
                )
                for event_type, model, think, details in events:
                    _write_event(conn, event_type, spec, model, think, details, now)
                for row in rows:
                    known_models.add((row["model"], row["think"]))
                imported += 1
                checked += 1

            if is_baseline and imported:
                set_meta(conn, "baseline_at", now)

            errors = fetch_errors
            status = "ok" if not errors else ("partial" if checked else "error")
            duration_ms = int((time.monotonic() - started) * 1000)
            set_meta(conn, "last_poll_at", now)
            set_meta(conn, "last_poll_status", status)
            set_meta(conn, "last_poll_duration_ms", str(duration_ms))
            set_meta(conn, "last_error", "；".join(errors[:3]) + (" 等" if len(errors) > 3 else ""))
            set_meta(conn, "poll_started_at", "")
            if checked and status != "error":
                set_meta(conn, "last_success_at", now)
        log.info(
            "poll complete: %s datasets=%d imported=%d errors=%d in %dms",
            status,
            len(specs),
            imported,
            len(fetch_errors),
            int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:
        conn.rollback()
        log.exception("failed to apply poll")
        _record_failure(conn, started, f"apply: {exc}")


def _fetch_dataset(fetcher, base_url: str, spec: dict, old: dict | None):
    try:
        result = fetcher(
            urljoin(base_url, spec["csv_path"]),
            old.get("etag") if old else None,
            old.get("last_modified") if old else None,
        )
        return spec, old, result, None
    except Exception as exc:
        return spec, old, None, f"{spec['label']}: {exc}"


def _record_failure(conn: sqlite3.Connection, started: float, message: str) -> None:
    now = iso(utc_now())
    try:
        with conn:
            set_meta(conn, "last_poll_at", now)
            set_meta(conn, "last_poll_status", "error")
            set_meta(conn, "last_error", message[:500])
            set_meta(conn, "last_poll_duration_ms", str(int((time.monotonic() - started) * 1000)))
            set_meta(conn, "poll_started_at", "")
    except Exception:
        log.exception("could not record poll failure")


class Monitor:
    """Single background worker; the API only reads its state."""

    def __init__(self, interval: int = POLL_INTERVAL, fetcher=fetch_http, base_url: str = BASE_URL):
        self.interval = interval
        self.fetcher = fetcher
        self.base_url = base_url
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._refresh_lock = threading.Lock()
        self._last_refresh = 0.0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="rankings-monitor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn = open_db()
                try:
                    poll_once(conn, self.fetcher, self.base_url)
                finally:
                    conn.close()
            except Exception as exc:
                log.exception("poll cycle crashed")
                try:
                    conn = open_db()
                    try:
                        _record_failure(conn, time.monotonic(), f"worker: {exc}")
                    finally:
                        conn.close()
                except Exception:
                    log.exception("could not persist worker failure")
            self._wake.wait(self.interval)
            self._wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def request_refresh(self) -> bool:
        now = time.monotonic()
        with self._refresh_lock:
            if now - self._last_refresh < 15:
                return False
            self._last_refresh = now
            self._wake.set()
            return True


def api_overview(conn: sqlite3.Connection) -> dict:
    meta = meta_map(conn)
    categories = [
        {
            "key": row["category"],
            "label": category_label(row["category"]),
            "datasets": row["count"],
        }
        for row in conn.execute(
            "SELECT category, COUNT(*) AS count FROM datasets "
            "WHERE active = 1 AND category IN (?, ?) GROUP BY category",
            MONITORED_CATEGORIES,
        )
    ]
    order = {key: index for index, key in enumerate(CATEGORY_ORDER)}
    categories.sort(key=lambda item: (order.get(item["key"], 99), item["key"]))
    status = meta.get("last_poll_status", "idle")
    next_poll_at = None
    last_poll_at = meta.get("last_poll_at") or None
    if last_poll_at and status != "running":
        try:
            moment = datetime.fromisoformat(last_poll_at.replace("Z", "+00:00"))
            next_poll_at = iso(moment + timedelta(seconds=POLL_INTERVAL))
        except ValueError:
            next_poll_at = None
    return {
        "source_url": SOURCE_URL,
        "poll_interval": POLL_INTERVAL,
        "status": status,
        "last_poll_at": last_poll_at,
        "last_success_at": meta.get("last_success_at") or None,
        "last_error": meta.get("last_error") or None,
        "next_poll_at": next_poll_at,
        "baseline_at": meta.get("baseline_at") or None,
        "datasets": conn.execute(
            "SELECT COUNT(*) FROM datasets WHERE active = 1 AND category IN (?, ?)",
            MONITORED_CATEGORIES,
        ).fetchone()[0],
        "models": conn.execute("SELECT COUNT(*) FROM models").fetchone()[0],
        "events": conn.execute(
            "SELECT COUNT(*) FROM events WHERE category IN (?, ?)",
            MONITORED_CATEGORIES,
        ).fetchone()[0],
        "categories": categories,
    }


def _query_int(query: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    raw = query.get(name, [str(default)])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def api_events(conn: sqlite3.Connection, query: dict) -> dict:
    event_type = query.get("type", ["all"])[0]
    category = query.get("category", ["all"])[0]
    if event_type != "all" and event_type not in EVENT_TYPES:
        raise BadRequest("unknown event type")
    if category != "all" and category not in CATEGORY_LABELS:
        raise BadRequest("unknown category")

    limit = _query_int(query, "limit", 50, 1, 100)
    offset = _query_int(query, "offset", 0, 0, 1_000_000)
    clauses = ["category IN (?, ?)"]
    params: list[str] = list(MONITORED_CATEGORIES)
    if event_type != "all":
        clauses.append("type = ?")
        params.append(event_type)
    if category != "all":
        clauses.append("category = ?")
        params.append(category)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total = conn.execute("SELECT COUNT(*) FROM events" + where, params).fetchone()[0]
    rows = conn.execute(
        "SELECT id, type, category, dataset_key, dataset_label, model, think, details, created_at "
        "FROM events" + where + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    items = []
    for row in rows:
        try:
            details = json.loads(row["details"])
        except json.JSONDecodeError:
            details = {}
        items.append(
            {
                "id": row["id"],
                "type": row["type"],
                "category": row["category"],
                "category_label": category_label(row["category"]),
                "dataset_key": row["dataset_key"],
                "dataset_label": row["dataset_label"],
                "model": row["model"],
                "think": row["think"],
                "model_label": display_model(row["model"], row["think"]),
                "details": details,
                "created_at": row["created_at"],
            }
        )
    return {"total": total, "items": items}


def api_leaderboard(conn: sqlite3.Connection, query: dict) -> dict:
    available = [
        row["category"]
        for row in conn.execute(
            "SELECT category, COUNT(*) AS count FROM datasets "
            "WHERE active = 1 AND category IN (?, ?) GROUP BY category",
            MONITORED_CATEGORIES,
        )
    ]
    available.sort(key=lambda key: (CATEGORY_ORDER.index(key) if key in CATEGORY_ORDER else 99, key))
    category = query.get("category", [""])[0]
    if category not in available:
        category = available[0] if available else "logic"

    datasets = [
        {
            "key": row["key"],
            "label": " · ".join(part for part in (row["report_date"], row["title"]) if part),
            "report_date": row["report_date"],
            "title": row["title"],
            "row_count": row["row_count"],
        }
        for row in conn.execute(
            "SELECT key, report_date, title, row_count FROM datasets "
            "WHERE active = 1 AND category = ? ORDER BY report_date DESC, key",
            (category,),
        )
    ]
    if not datasets:
        return {
            "category": category,
            "category_label": category_label(category),
            "datasets": [],
            "dataset": None,
            "score_fields": [],
            "rows": [],
        }

    selected_key = query.get("dataset", [""])[0]
    selected = next((item for item in datasets if item["key"] == selected_key), datasets[0])
    row = conn.execute(
        "SELECT score_fields FROM datasets WHERE key = ?", (selected["key"],)
    ).fetchone()
    try:
        score_fields = json.loads(row["score_fields"]) if row else []
    except json.JSONDecodeError:
        score_fields = []

    entries = []
    for entry in conn.execute(
        "SELECT model, think, position, scores FROM entries WHERE dataset_key = ? "
        "ORDER BY position, model",
        (selected["key"],),
    ):
        try:
            scores = json.loads(entry["scores"])
        except json.JSONDecodeError:
            scores = {}
        entries.append(
            {
                "model": entry["model"],
                "think": entry["think"],
                "model_label": display_model(entry["model"], entry["think"]),
                "position": entry["position"],
                "scores": scores,
            }
        )
    return {
        "category": category,
        "category_label": category_label(category),
        "datasets": datasets,
        "dataset": selected,
        "score_fields": score_fields,
        "rows": entries,
    }


MONITOR: Monitor | None = None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'; object-src 'none'",
        )
        super().end_headers()

    def send_json(self, payload: dict, status: int = 200, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve(self, function) -> None:
        try:
            with closing(open_db()) as conn:
                payload = function(conn)
            self.send_json(payload)
        except BadRequest as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception:
            log.exception("API request failed")
            self.send_json({"error": "internal_error"}, 500)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/healthz":
            self.send_json({"status": "ok"})
        elif parsed.path == "/api/overview":
            self._serve(api_overview)
        elif parsed.path == "/api/events":
            self._serve(lambda conn: api_events(conn, query))
        elif parsed.path == "/api/leaderboard":
            self._serve(lambda conn: api_leaderboard(conn, query))
        elif parsed.path.startswith("/api/"):
            self.send_json({"error": "not_found"}, 404)
        else:
            super().do_GET()

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(min(length, 1_000_000))
        if parsed.path != "/api/refresh":
            self.send_json({"error": "not_found"}, 404)
            return
        if MONITOR is None or not MONITOR.request_refresh():
            self.send_json(
                {"error": "refresh_rate_limited", "retry_after": 15},
                429,
                {"Retry-After": "15"},
            )
            return
        self.send_json({"status": "queued"}, 202)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not STATIC_DIR.is_dir():
        raise SystemExit(f"static directory not found: {STATIC_DIR}")
    init_db()
    global MONITOR
    MONITOR = Monitor()
    MONITOR.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True

    def shutdown(signum, frame):
        log.info("received signal %s, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.info(
        "listening on :%d, source=%s, interval=%ds, db=%s",
        PORT,
        SOURCE_URL,
        POLL_INTERVAL,
        DB_PATH,
    )
    try:
        server.serve_forever()
    finally:
        MONITOR.stop()
        server.server_close()
        log.info("stopped")


if __name__ == "__main__":
    main()
