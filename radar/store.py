from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

from .sources import SOURCES, is_model_research


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    region TEXT NOT NULL,
    content_type TEXT NOT NULL,
    raw_score REAL NOT NULL DEFAULT 0,
    engagement INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}',
    fingerprint TEXT NOT NULL,
    UNIQUE(source_key, external_id)
);
CREATE INDEX IF NOT EXISTS idx_items_published ON items(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_fingerprint ON items(fingerprint);

CREATE TABLE IF NOT EXISTS source_status (
    source_key TEXT PRIMARY KEY,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    last_item_count INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS refresh_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    source_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS source_cursors (
    source_key TEXT PRIMARY KEY,
    cursor TEXT NOT NULL DEFAULT '',
    last_request_at TEXT,
    request_day TEXT NOT NULL DEFAULT '',
    request_count INTEGER NOT NULL DEFAULT 0,
    resource_day TEXT NOT NULL DEFAULT '',
    resource_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_summaries (
    summary_date TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    model TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    generated_at TEXT NOT NULL
);
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            # Upgrade previously collected X posts without spending another API
            # request. New records use article/long-form fields for the richer
            # classification; this gives older high-engagement posts a sensible
            # discussion classification after deployment.
            conn.execute(
                """
                UPDATE items
                SET content_type='discussion'
                WHERE source_key='x-ai'
                  AND content_type='news'
                  AND (engagement >= 300 OR LENGTH(summary) >= 180)
                """
            )
            for source in SOURCES:
                conn.execute(
                    "INSERT OR IGNORE INTO source_status(source_key) VALUES (?)",
                    (source.key,),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO source_cursors(source_key) VALUES (?)",
                    (source.key,),
                )

    def begin_source_request(
        self,
        key: str,
        *,
        min_interval_minutes: int,
        max_calls_per_day: int,
    ) -> tuple[bool, str, str]:
        now = utc_now()
        today = now.date().isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM source_cursors WHERE source_key=?",
                (key,),
            ).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO source_cursors(source_key) VALUES (?)",
                    (key,),
                )
                row = conn.execute(
                    "SELECT * FROM source_cursors WHERE source_key=?",
                    (key,),
                ).fetchone()

            request_count = row["request_count"] if row["request_day"] == today else 0
            if request_count >= max_calls_per_day:
                return False, row["cursor"], "daily request budget reached"

            if row["last_request_at"]:
                last_request = _parse_iso(row["last_request_at"])
                elapsed_minutes = (now - last_request).total_seconds() / 60
                if elapsed_minutes < min_interval_minutes:
                    return False, row["cursor"], "minimum request interval not reached"

            conn.execute(
                """
                UPDATE source_cursors
                SET last_request_at=?, request_day=?, request_count=?
                WHERE source_key=?
                """,
                (now.isoformat(), today, request_count + 1, key),
            )
            return True, row["cursor"], ""

    def finish_source_request(self, key: str, *, cursor: str, resource_count: int) -> None:
        today = utc_now().date().isoformat()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT resource_day, resource_count FROM source_cursors WHERE source_key=?",
                (key,),
            ).fetchone()
            previous = row["resource_count"] if row and row["resource_day"] == today else 0
            conn.execute(
                """
                UPDATE source_cursors
                SET cursor=CASE WHEN ?='' THEN cursor ELSE ? END,
                    resource_day=?, resource_count=?
                WHERE source_key=?
                """,
                (cursor, cursor, today, previous + max(0, resource_count), key),
            )

    def start_run(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO refresh_runs(started_at, source_count) VALUES (?, ?)",
                (utc_now().isoformat(), len(SOURCES)),
            )
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, success_count: int, item_count: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE refresh_runs SET finished_at=?, success_count=?, item_count=? WHERE id=?",
                (utc_now().isoformat(), success_count, item_count, run_id),
            )
            conn.execute(
                "DELETE FROM items WHERE published_at < ?",
                ((utc_now() - timedelta(days=30)).isoformat(),),
            )
            conn.execute(
                "DELETE FROM daily_summaries WHERE summary_date < ?",
                ((utc_now() - timedelta(days=30)).date().isoformat(),),
            )

    def update_source(self, key: str, *, count: int, latency_ms: int, error: str = "") -> None:
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE source_status
                SET last_attempt_at=?, last_success_at=CASE WHEN ?='' THEN ? ELSE last_success_at END,
                    last_error=?, last_item_count=?, latency_ms=?
                WHERE source_key=?
                """,
                (now, error, now, error[:500], count, latency_ms, key),
            )

    def upsert_items(self, items: Iterable[dict[str, Any]]) -> int:
        count = 0
        sql = """
            INSERT INTO items(
                source_key, external_id, title, url, canonical_url, summary, author,
                published_at, collected_at, region, content_type, raw_score,
                engagement, tags, metadata, fingerprint
            ) VALUES (
                :source_key, :external_id, :title, :url, :canonical_url, :summary, :author,
                :published_at, :collected_at, :region, :content_type, :raw_score,
                :engagement, :tags, :metadata, :fingerprint
            )
            ON CONFLICT(source_key, external_id) DO UPDATE SET
                title=excluded.title, url=excluded.url, canonical_url=excluded.canonical_url,
                summary=excluded.summary, author=excluded.author, published_at=excluded.published_at,
                collected_at=CASE WHEN excluded.source_key='x-ai' THEN excluded.collected_at ELSE items.collected_at END,
                content_type=excluded.content_type, raw_score=excluded.raw_score, engagement=excluded.engagement,
                tags=excluded.tags, metadata=excluded.metadata, fingerprint=excluded.fingerprint
        """
        with self.connect() as conn:
            for item in items:
                payload = dict(item)
                payload["tags"] = json.dumps(payload.get("tags", []), ensure_ascii=False)
                payload["metadata"] = json.dumps(payload.get("metadata", {}), ensure_ascii=False)
                conn.execute(sql, payload)
                count += 1
        return count

    def query_items(
        self,
        *,
        hours: int = 24,
        region: str = "all",
        content_type: str = "all",
        query: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        cutoff = (utc_now() - timedelta(hours=hours)).isoformat()
        effective_time = "CASE WHEN content_type IN ('project', 'model') THEN MAX(published_at, collected_at) ELSE published_at END"
        conditions = [f"{effective_time} >= ?"]
        params: list[Any] = [cutoff]
        if region != "all":
            conditions.append("region = ?")
            params.append(region)
        if content_type != "all":
            conditions.append("content_type = ?")
            params.append(content_type)
        if query:
            conditions.append("(title LIKE ? OR summary LIKE ? OR tags LIKE ?)")
            needle = f"%{query}%"
            params.extend([needle, needle, needle])

        sql = f"SELECT *, {effective_time} AS effective_at FROM items WHERE {' AND '.join(conditions)} ORDER BY effective_at DESC LIMIT 3000"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            grouped[row["fingerprint"] or row["canonical_url"]].append(row)

        now = utc_now()
        ranked: list[dict[str, Any]] = []
        for group in grouped.values():
            best = max(
                group,
                key=lambda row: (row["source_key"] == "x-ai", row["raw_score"], row["engagement"]),
            )
            published = _parse_iso(best["effective_at"])
            age_hours = max(0.0, (now - published).total_seconds() / 3600)
            source = next((s for s in SOURCES if s.key == best["source_key"]), None)
            source_weight = source.weight if source else 1.0
            freshness = max(0.0, 34.0 * (1.0 - age_hours / max(hours, 24)))
            engagement = min(28.0, math.log1p(max(0, best["engagement"])) * 4.5)
            corroboration = min(24.0, (len({row["source_key"] for row in group}) - 1) * 12.0)
            quality = source_weight * 12.0 + min(12.0, float(best["raw_score"]))
            score = round(freshness + engagement + corroboration + quality, 1)
            metadata = json.loads(best["metadata"])
            metrics = metadata.get("metrics", {}) if isinstance(metadata, dict) else {}
            discussion_score = float(metadata.get("discussion_score", 0) or 0) if isinstance(metadata, dict) else 0.0
            tags = json.loads(best["tags"])
            model_relevant = best["content_type"] != "paper" or is_model_research(
                best["title"], best["summary"], tags
            )
            is_research_source = bool(source and source.kind == "paper")
            if best["content_type"] == "paper" and (not model_relevant or not is_research_source):
                continue
            paper_metrics = {
                "upvotes": int(metadata.get("upvotes", 0) or 0),
                "comments": int(metadata.get("comments", 0) or 0),
                "github_stars": int(metadata.get("github_stars", 0) or 0),
            } if isinstance(metadata, dict) else {}
            sources = []
            ordered_group = [best, *(row for row in group if row["id"] != best["id"])]
            for row in ordered_group:
                item_source = next((s for s in SOURCES if s.key == row["source_key"]), None)
                sources.append(
                    {
                        "key": row["source_key"],
                        "name": item_source.name if item_source else row["source_key"],
                        "url": row["url"],
                    }
                )
            ranked.append(
                {
                    "id": best["id"],
                    "source_key": best["source_key"],
                    "title": best["title"],
                    "url": best["url"],
                    "summary": best["summary"],
                    "author": best["author"],
                    "published_at": best["published_at"],
                    "discovered_at": best["collected_at"],
                    "effective_at": best["effective_at"],
                    "region": best["region"],
                    "content_type": best["content_type"],
                    "score": score,
                    "engagement": best["engagement"],
                    "metrics": metrics,
                    "discussion_score": discussion_score,
                    "paper_metrics": paper_metrics,
                    "model_relevant": model_relevant,
                    "tags": tags,
                    "sources": sources,
                    "source_count": len({row["source_key"] for row in group}),
                }
            )
        def priority_tier(item: dict[str, Any]) -> int:
            if item["source_key"] != "x-ai":
                return 0
            return 1

        ranked.sort(
            key=lambda item: (
                priority_tier(item),
                item["discussion_score"] if item["source_key"] == "x-ai" else item["score"],
                "重点账号" in item["tags"],
                item["score"],
                item["effective_at"],
            ),
            reverse=True,
        )
        max_dashboard_items = 50 + max(0, len(SOURCES) - 1) * 30
        result_limit = max(1, min(limit, max_dashboard_items))
        source_counts: dict[str, int] = defaultdict(int)
        selected: list[dict[str, Any]] = []
        for item in ranked:
            source_limit = 50 if item["source_key"] == "x-ai" else 30
            if source_counts[item["source_key"]] >= source_limit:
                continue
            selected.append(item)
            source_counts[item["source_key"]] += 1
            if len(selected) >= result_limit:
                break
        return selected

    def list_sources(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            statuses = {
                row["source_key"]: dict(row)
                for row in conn.execute("SELECT * FROM source_status").fetchall()
            }
            cursors = {
                row["source_key"]: dict(row)
                for row in conn.execute(
                    "SELECT source_key, request_day, request_count, resource_day, resource_count FROM source_cursors"
                ).fetchall()
            }
        result = []
        for source in SOURCES:
            status = statuses.get(source.key, {})
            result.append(
                {
                    "key": source.key,
                    "name": source.name,
                    "region": source.region,
                    "kind": source.kind,
                    "url": source.url,
                    **status,
                    **cursors.get(source.key, {}),
                }
            )
        return result

    def get_daily_summary(self, summary_date: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM daily_summaries WHERE summary_date=?",
                (summary_date,),
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return {
            "summary_date": row["summary_date"],
            "content_hash": row["content_hash"],
            "payload": payload,
            "model": row["model"],
            "item_count": row["item_count"],
            "generated_at": row["generated_at"],
        }

    def save_daily_summary(
        self,
        *,
        summary_date: str,
        content_hash: str,
        payload: dict[str, Any],
        model: str,
        item_count: int,
        generated_at: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_summaries(
                    summary_date, content_hash, payload, model, item_count, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(summary_date) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    payload=excluded.payload,
                    model=excluded.model,
                    item_count=excluded.item_count,
                    generated_at=excluded.generated_at
                """,
                (
                    summary_date,
                    content_hash,
                    json.dumps(payload, ensure_ascii=False),
                    model,
                    item_count,
                    generated_at,
                ),
            )

    def summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN
                           (content_type IN ('project', 'model') AND MAX(published_at, collected_at) >= ?)
                           OR (content_type NOT IN ('project', 'model') AND published_at >= ?)
                           THEN 1 ELSE 0 END) AS today,
                       MAX(collected_at) AS updated_at
                FROM items
                """,
                (
                    (utc_now() - timedelta(hours=24)).isoformat(),
                    (utc_now() - timedelta(hours=24)).isoformat(),
                ),
            ).fetchone()
            last_run = conn.execute(
                "SELECT * FROM refresh_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "total": row["total"] or 0,
            "today": row["today"] or 0,
            "updated_at": row["updated_at"],
            "last_run": dict(last_run) if last_run else None,
        }


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
