from __future__ import annotations

import gzip
import hashlib
import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from .sources import SOURCES, X_AI_QUERY, X_PRIORITY_QUERIES, Source
from .store import Store, utc_now


USER_AGENT = "AIRadar/0.1 (+local personal research dashboard)"
REDDIT_FEED_URL = "https://www.reddit.com/r/MachineLearning+LocalLLaMA/.rss?limit=50"
REDDIT_SUBREDDITS = {
    "reddit-ml": "MachineLearning",
    "reddit-localllama": "LocalLLaMA",
}
REDDIT_CACHE_SECONDS = 120
_REDDIT_FEED_LOCK = threading.Lock()
_REDDIT_FEED_CACHE: dict[str, Any] = {"fetched_at": 0.0, "items": []}
AI_TERMS = (
    " ai ", "artificial intelligence", "machine learning", "deep learning", "llm",
    "gpt", "claude", "gemini", "openai", "anthropic", "deepseek", "qwen", "mistral",
    "llama", "agent", "mcp", "diffusion", "transformer", "robot", "copilot",
    "人工智能", "大模型", "机器学习", "深度学习", "智能体", "机器人", "生成式", "模型",
)
PROJECT_TERMS = ("github", "open source", "open-source", "repo", "tool", "framework", "开源", "项目", "工具", "框架")
MODEL_TERMS = ("model", "llm", "vlm", "embedding", "diffusion", "模型")
PAPER_TERMS = ("paper", "benchmark", "arxiv", "论文", "基准")


@dataclass(slots=True)
class CollectionBatch:
    items: list[dict[str, Any]]
    cursor: str = ""
    skipped: str = ""
    resource_count: int = 0


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def clean_html(value: str | None, limit: int = 420) -> str:
    if not value:
        return ""
    parser = TextExtractor()
    try:
        parser.feed(html.unescape(value))
        text = " ".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html.unescape(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


def parse_datetime(value: Any) -> datetime:
    if not value:
        return utc_now()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError, OverflowError):
        return utc_now()


def canonicalize_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(key, val) for key, val in query if not key.lower().startswith("utm_") and key.lower() not in {"ref", "source", "s"}]
        path = parsed.path.rstrip("/") or "/"
        return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, urllib.parse.urlencode(query), ""))
    except ValueError:
        return value


def title_fingerprint(title: str) -> str:
    normalized = html.unescape(title).lower()
    normalized = re.sub(r"\[[^]]+] |\([^)]*\)$", " ", normalized)
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", normalized)
    return hashlib.sha1(normalized[:180].encode("utf-8")).hexdigest()


def is_ai_related(title: str, summary: str = "") -> bool:
    haystack = f" {title} {summary} ".lower()
    return any(term in haystack for term in AI_TERMS)


def classify(title: str, summary: str, default: str) -> str:
    if default in {"project", "model", "paper", "discussion"}:
        return default
    text = title.lower()
    if any(term in text for term in PROJECT_TERMS):
        return "project"
    if any(term in text for term in PAPER_TERMS):
        return "paper"
    if any(term in text for term in MODEL_TERMS):
        return "model"
    return "news"


def _request_json(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    proxy_url: str = "",
) -> Any:
    return json.loads(_request_bytes(url, headers, proxy_url=proxy_url).decode("utf-8"))


def _request_bytes(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    proxy_url: str = "",
) -> bytes:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/atom+xml, application/rss+xml, application/xml, text/xml, */*",
        "Accept-Encoding": "gzip",
        **(headers or {}),
    }
    request = urllib.request.Request(url, headers=request_headers)
    opener = None
    if proxy_url:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
    for attempt in range(2):
        try:
            open_request = opener.open if opener else urllib.request.urlopen
            with open_request(request, timeout=18) as response:
                payload = response.read(3_000_000)
                if response.headers.get("Content-Encoding") == "gzip":
                    payload = gzip.decompress(payload)
                return payload
        except urllib.error.URLError as exc:
            if attempt == 0 and "reset" in str(exc).lower():
                time.sleep(0.4)
                continue
            if attempt == 0 and isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                retry_after = exc.headers.get("Retry-After") or exc.headers.get("X-RateLimit-Reset") or "2"
                try:
                    delay = max(1.0, min(12.0, float(retry_after)))
                except ValueError:
                    delay = 2.0
                time.sleep(delay)
                continue
            raise
    raise RuntimeError("unreachable")


def _base_item(source: Source, *, external_id: str, title: str, url: str, summary: str = "", author: str = "", published_at: Any = None, engagement: int = 0, raw_score: float = 0, tags: list[str] | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    published = parse_datetime(published_at)
    return {
        "source_key": source.key,
        "external_id": str(external_id or url or title_fingerprint(title)),
        "title": clean_html(title, 260),
        "url": url,
        "canonical_url": canonicalize_url(url),
        "summary": clean_html(summary),
        "author": clean_html(author, 100),
        "published_at": published.astimezone(UTC).isoformat(),
        "collected_at": utc_now().isoformat(),
        "region": source.region,
        "content_type": classify(title, summary, source.kind),
        "raw_score": float(raw_score or 0),
        "engagement": max(0, int(engagement or 0)),
        "tags": list(dict.fromkeys((tags or [])[:8])),
        "metadata": metadata or {},
        "fingerprint": title_fingerprint(title),
    }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element: ET.Element, *names: str) -> str:
    wanted = set(names)
    for child in element:
        if _local_name(child.tag) in wanted:
            if child.text and child.text.strip():
                return child.text.strip()
            if child.attrib.get("href"):
                return child.attrib["href"]
    return ""


def _extract_loose_rss(payload: bytes) -> list[dict[str, str]]:
    text = payload.decode("utf-8", errors="replace")
    blocks = re.findall(r"<(?:item|entry)\b[^>]*>(.*?)</(?:item|entry)>", text, flags=re.I | re.S)

    def field(block: str, *names: str) -> str:
        for name in names:
            match = re.search(
                rf"<(?:[\w-]+:)?{name}\b[^>]*>(.*?)</(?:[\w-]+:)?{name}>",
                block,
                flags=re.I | re.S,
            )
            if match:
                value = re.sub(r"^<!\[CDATA\[|\]\]>$", "", match.group(1).strip())
                return value.strip()
        return ""

    result = []
    for block in blocks:
        link = field(block, "link")
        if not link:
            link_match = re.search(r"<link\b[^>]*href=[\"']([^\"']+)", block, flags=re.I)
            link = link_match.group(1) if link_match else ""
        result.append({
            "title": field(block, "title"),
            "link": clean_html(link, 1000),
            "summary": field(block, "description", "summary", "encoded", "content"),
            "id": field(block, "guid", "id"),
            "author": field(block, "author", "creator"),
            "published": field(block, "pubDate", "published", "updated", "date"),
        })
    return result


def collect_rss(source: Source) -> list[dict[str, Any]]:
    payload = _request_bytes(source.url)
    try:
        root = ET.fromstring(payload)
        entries: list[ET.Element] = [el for el in root.iter() if _local_name(el.tag) in {"item", "entry"}]
    except ET.ParseError:
        entries = []
        loose_entries = _extract_loose_rss(payload)

    items = []
    normalized_entries: list[dict[str, Any]] = []
    for entry in entries[:50]:
        link = _child_text(entry, "link")
        normalized_entries.append({
            "title": _child_text(entry, "title"),
            "link": link,
            "summary": _child_text(entry, "description", "summary", "content", "encoded"),
            "id": _child_text(entry, "guid", "id"),
            "author": _child_text(entry, "author", "creator"),
            "published": _child_text(entry, "pubdate", "published", "updated", "date"),
            "tags": [child.text.strip() for child in entry if _local_name(child.tag) == "category" and child.text][:8],
        })
    if not entries:
        normalized_entries = [{**entry, "tags": []} for entry in loose_entries[:50]]

    for entry in normalized_entries:
        title = entry["title"]
        link = entry["link"]
        summary = entry["summary"]
        if not title or not link or (source.filter_ai and not is_ai_related(title, summary)):
            continue
        items.append(_base_item(
            source,
            external_id=entry["id"] or link,
            title=title,
            url=link,
            summary=summary,
            author=entry["author"],
            published_at=entry["published"],
            tags=entry["tags"],
        ))
    return items


def collect_github(source: Source) -> list[dict[str, Any]]:
    since = (utc_now() - timedelta(days=14)).date().isoformat()
    params = urllib.parse.urlencode({
        "q": f"topic:llm created:>={since} stars:>3",
        "sort": "stars",
        "order": "desc",
        "per_page": 40,
    })
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    payload = _request_json(f"{source.url}?{params}", headers)
    result = []
    for repo in payload.get("items", []):
        result.append(_base_item(
            source,
            external_id=repo.get("id"),
            title=repo.get("full_name", ""),
            url=repo.get("html_url", ""),
            summary=repo.get("description", ""),
            author=(repo.get("owner") or {}).get("login", ""),
            published_at=repo.get("created_at") or repo.get("pushed_at"),
            engagement=repo.get("stargazers_count", 0),
            raw_score=min(12, repo.get("stargazers_count", 0) / 25),
            tags=repo.get("topics", []),
            metadata={"stars": repo.get("stargazers_count", 0), "language": repo.get("language")},
        ))
    return result


def collect_huggingface(source: Source) -> list[dict[str, Any]]:
    payload = _request_json(source.url)
    result = []
    entity = "spaces" if source.kind == "project" else "models"
    for item in payload[:40]:
        item_id = item.get("id") or item.get("modelId")
        if not item_id:
            continue
        likes = item.get("likes", 0) or 0
        downloads = item.get("downloads", 0) or 0
        engagement = likes * 8 + min(downloads, 50000) // 100
        result.append(_base_item(
            source,
            external_id=item_id,
            title=item_id,
            url=f"https://huggingface.co/{'spaces/' if entity == 'spaces' else ''}{item_id}",
            summary=item.get("cardData", {}).get("short_description", "") if isinstance(item.get("cardData"), dict) else "",
            author=item_id.split("/", 1)[0] if "/" in item_id else "",
            published_at=item.get("lastModified") or item.get("createdAt"),
            engagement=engagement,
            raw_score=min(12, (item.get("trendingScore", 0) or 0) / 5 + likes / 20),
            tags=item.get("tags", []),
            metadata={"likes": likes, "downloads": downloads, "pipeline": item.get("pipeline_tag")},
        ))
    return result


def collect_hf_papers(source: Source) -> list[dict[str, Any]]:
    payload = _request_json(source.url)
    result = []
    for entry in payload[:40]:
        paper = entry.get("paper", entry)
        paper_id = paper.get("id") or paper.get("paperId")
        title = paper.get("title") or entry.get("title")
        if not paper_id or not title:
            continue
        authors = paper.get("authors") or []
        author_text = ", ".join(a.get("name", "") if isinstance(a, dict) else str(a) for a in authors[:3])
        result.append(_base_item(
            source,
            external_id=paper_id,
            title=title,
            url=f"https://huggingface.co/papers/{paper_id}",
            summary=paper.get("summary") or paper.get("abstract", ""),
            author=author_text,
            published_at=paper.get("publishedAt") or entry.get("publishedAt"),
            engagement=entry.get("upvotes", 0) or paper.get("upvotes", 0),
            raw_score=min(12, (entry.get("upvotes", 0) or 0) / 5),
            tags=["paper"],
        ))
    return result


def collect_hackernews(source: Source) -> list[dict[str, Any]]:
    since = int((utc_now() - timedelta(days=3)).timestamp())
    params = urllib.parse.urlencode({
        "query": "AI OR LLM OR GPT OR Claude",
        "tags": "story",
        "numericFilters": f"created_at_i>{since}",
        "hitsPerPage": 50,
    })
    payload = _request_json(f"{source.url}?{params}")
    result = []
    for hit in payload.get("hits", []):
        title = hit.get("title") or hit.get("story_title")
        if not title or not is_ai_related(title):
            continue
        object_id = hit.get("objectID")
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
        points = hit.get("points", 0) or 0
        comments = hit.get("num_comments", 0) or 0
        result.append(_base_item(
            source,
            external_id=object_id,
            title=title,
            url=url,
            summary=f"{points} points · {comments} comments",
            author=hit.get("author", ""),
            published_at=hit.get("created_at"),
            engagement=points + comments * 2,
            raw_score=min(12, (points + comments) / 20),
            tags=["Hacker News"],
            metadata={"discussion_url": f"https://news.ycombinator.com/item?id={object_id}", "points": points, "comments": comments},
        ))
    return result


def collect_arxiv(source: Source) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({
        "search_query": "cat:cs.AI OR cat:cs.LG OR cat:cs.CL",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": 0,
        "max_results": 35,
    })
    dynamic = Source(source.key, source.name, source.region, source.kind, f"{source.url}?{params}", "rss", source.weight)
    return collect_rss(dynamic)


def collect_devto(source: Source) -> list[dict[str, Any]]:
    payload = _request_json(source.url)
    return [
        _base_item(
            source,
            external_id=item.get("id"),
            title=item.get("title", ""),
            url=item.get("url", ""),
            summary=item.get("description", ""),
            author=(item.get("user") or {}).get("name", ""),
            published_at=item.get("published_at"),
            engagement=(item.get("positive_reactions_count", 0) or 0) + (item.get("comments_count", 0) or 0) * 2,
            raw_score=min(12, (item.get("public_reactions_count", 0) or 0) / 10),
            tags=item.get("tag_list", []),
        )
        for item in payload
        if item.get("title") and item.get("url")
    ]


def collect_reddit(source: Source) -> list[dict[str, Any]]:
    subreddit = REDDIT_SUBREDDITS.get(source.key)
    if not subreddit:
        raise ValueError(f"Unknown Reddit source: {source.key}")

    with _REDDIT_FEED_LOCK:
        age = time.monotonic() - float(_REDDIT_FEED_CACHE["fetched_at"])
        if not _REDDIT_FEED_CACHE["items"] or age >= REDDIT_CACHE_SECONDS:
            combined_source = Source(
                "reddit-combined",
                "Reddit",
                "global",
                "discussion",
                REDDIT_FEED_URL,
                "rss",
            )
            _REDDIT_FEED_CACHE["items"] = collect_rss(combined_source)
            _REDDIT_FEED_CACHE["fetched_at"] = time.monotonic()
        feed_items = [dict(item) for item in _REDDIT_FEED_CACHE["items"]]

    result = []
    subreddit_path = f"/r/{subreddit.lower()}/"
    for item in feed_items:
        if subreddit_path not in item["url"].lower():
            continue
        item["source_key"] = source.key
        item["metadata"] = {**item["metadata"], "subreddit": subreddit, "via": "rss"}
        result.append(item)
    return result


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def collect_x(source: Source, store: Store) -> CollectionBatch:
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if not token:
        raise RuntimeError("X_BEARER_TOKEN is not configured")

    max_results = _env_int("X_MAX_RESULTS", 50, 10, 100)
    max_calls = _env_int("X_MAX_CALLS_PER_DAY", 1, 1, 24)
    min_interval = _env_int("X_MIN_INTERVAL_MINUTES", 1440, 30, 1440)
    allowed, cursor, reason = store.begin_source_request(
        source.key,
        min_interval_minutes=min_interval,
        max_calls_per_day=max_calls,
    )
    if not allowed:
        return CollectionBatch([], skipped=reason)

    if source.key == "x-ai":
        query_templates = [
            (X_PRIORITY_QUERIES[0], True),
            (X_PRIORITY_QUERIES[1], True),
            (X_AI_QUERY, False),
        ]
    else:
        query_templates = [(source.query, False)]

    request_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    proxy_url = os.environ.get("X_HTTPS_PROXY", "").strip()
    result_by_id: dict[str, dict[str, Any]] = {}
    newest_ids: list[str] = []
    resource_count = 0
    next_pages: list[tuple[str, bool, str]] = []

    # For the default 50-resource budget, reserve two 20-resource requests for
    # watched accounts and ten resources for broader AI discovery. Pagination
    # can fill unused capacity without exceeding the daily paid-resource cap.
    if source.key == "x-ai" and max_results >= 30:
        discovery_size = 10
        priority_total = max_results - discovery_size
        first_priority_size = priority_total // 2
        second_priority_size = priority_total - first_priority_size
        query_plan = [
            (X_PRIORITY_QUERIES[0], first_priority_size, True, ""),
            (X_PRIORITY_QUERIES[1], second_priority_size, True, ""),
            (X_AI_QUERY, discovery_size, False, ""),
        ]
    else:
        query_plan = [
            (query, max_results, priority, "")
            for query, priority in query_templates
        ]

    while query_plan and resource_count < max_results:
        query, requested_results, priority, pagination_token = query_plan.pop(0)
        query_max_results = min(requested_results, max_results - resource_count)
        if query_max_results < 10:
            break
        params = {
            "query": query,
            "max_results": query_max_results,
            "sort_order": "relevancy",
            "tweet.fields": "id,text,author_id,created_at,lang,public_metrics,entities,referenced_tweets",
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        elif source.key != "x-ai" and cursor:
            params["since_id"] = cursor
        elif source.key != "x-ai":
            params["start_time"] = (
                utc_now() - timedelta(hours=24)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        payload = _request_json(
            f"{source.url}?{urllib.parse.urlencode(params)}",
            request_headers,
            proxy_url=proxy_url,
        )
        posts = payload.get("data", [])
        resource_count += len(posts)
        newest_id = str(payload.get("meta", {}).get("newest_id", ""))
        if newest_id:
            newest_ids.append(newest_id)
        next_token = str(payload.get("meta", {}).get("next_token", ""))
        if next_token:
            next_pages.append((query, priority, next_token))

        for post in posts:
            post_id = str(post.get("id", ""))
            text = clean_html(post.get("text", ""), 420)
            if not post_id or not text:
                continue
            metrics = post.get("public_metrics") or {}
            engagement = (
                int(metrics.get("like_count", 0) or 0)
                + int(metrics.get("reply_count", 0) or 0) * 2
                + int(metrics.get("retweet_count", 0) or 0) * 3
                + int(metrics.get("quote_count", 0) or 0) * 3
                + int(metrics.get("bookmark_count", 0) or 0) * 2
            )
            language = str(post.get("lang", ""))
            tags = ["X"]
            if priority:
                tags.append("重点账号")
            if language:
                tags.append(language)
            item = _base_item(
                source,
                external_id=post_id,
                title=text[:260],
                url=f"https://x.com/i/web/status/{post_id}",
                summary=text,
                author="X 重点账号" if priority else "X",
                published_at=post.get("created_at"),
                engagement=engagement,
                raw_score=min(12, engagement / 50 + (3 if priority else 0)),
                tags=tags,
                metadata={
                    "author_id": post.get("author_id"),
                    "language": language,
                    "metrics": metrics,
                    "query_group": "priority" if priority else "discovery",
                },
            )
            if language == "zh":
                item["region"] = "china"
            existing = result_by_id.get(post_id)
            if existing is None or priority:
                result_by_id[post_id] = item

        if not query_plan and resource_count < max_results and next_pages:
            next_query, next_priority, next_token = next_pages.pop(0)
            remaining = max_results - resource_count
            if remaining >= 10:
                query_plan.append((next_query, remaining, next_priority, next_token))

    result = list(result_by_id.values())
    if not newest_ids and result:
        newest_ids = [item["external_id"] for item in result]
    newest_id = max(newest_ids, key=int) if newest_ids else ""
    return CollectionBatch(result, cursor=newest_id, resource_count=resource_count)


ADAPTERS = {
    "rss": collect_rss,
    "github": collect_github,
    "huggingface": collect_huggingface,
    "hf_papers": collect_hf_papers,
    "hackernews": collect_hackernews,
    "arxiv": collect_arxiv,
    "devto": collect_devto,
    "reddit": collect_reddit,
}


def collect_source(source: Source, store: Store) -> CollectionBatch:
    if source.adapter == "x":
        return collect_x(source, store)
    return CollectionBatch(ADAPTERS[source.adapter](source)[:30])


def refresh_all(store: Store, max_workers: int = 6) -> dict[str, Any]:
    run_id = store.start_run()
    total_items = 0
    successes = 0
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(collect_source, source, store): (source, time.monotonic()) for source in SOURCES}
        for future in as_completed(futures):
            source, started = futures[future]
            try:
                batch = future.result()
                if batch.skipped:
                    successes += 1
                    results.append({
                        "source": source.key,
                        "count": 0,
                        "ok": True,
                        "skipped": batch.skipped,
                    })
                    continue
                inserted = store.upsert_items(batch.items)
                if source.adapter == "x":
                    store.finish_source_request(
                        source.key,
                        cursor=batch.cursor,
                        resource_count=batch.resource_count,
                    )
                latency_ms = int((time.monotonic() - started) * 1000)
                store.update_source(source.key, count=inserted, latency_ms=latency_ms)
                total_items += inserted
                successes += 1
                results.append({"source": source.key, "count": inserted, "ok": True})
            except Exception as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                message = f"{type(exc).__name__}: {exc}"
                store.update_source(source.key, count=0, latency_ms=latency_ms, error=message)
                results.append({"source": source.key, "count": 0, "ok": False, "error": message})
    store.finish_run(run_id, successes, total_items)
    return {"run_id": run_id, "successes": successes, "sources": len(SOURCES), "items": total_items, "results": results}
