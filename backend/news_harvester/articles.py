"""
02_scrape_news_by_volume.py

Fast direct-to-Elasticsearch volume-weighted GDELT article scraper.

Reads volume rows from gdelt_news_volume_raw.
Writes only to gdelt_news_raw.
Document fields:
- index_name
- id
- time
- title
- text
- raw_html
"""

import argparse
import concurrent.futures
import json
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit
from backend.common.logging import get_logger
from backend.news_harvester.discovery import discover
log = get_logger("news_scraper")
from typing import Any, Dict, List, Optional, Tuple

import requests

from backend.news_harvester import config
from backend.news_harvester.es_client import bulk_upsert, doc_exists, ensure_indexes, search_all, stable_hash


ARTICLE_WORKERS = int(getattr(config, "ARTICLE_WORKERS", 5))
GDELT_ARTLIST_MAX_RETRIES = min(int(getattr(config, "MAX_RETRIES", 3)), 2)
ARTICLE_CONNECT_TIMEOUT = min(int(getattr(config, "ARTICLE_TIMEOUT_SECONDS", 8)), 8)


class ArticleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.current_tag: Optional[str] = None
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "canvas", "iframe"}:
            self.skip_depth += 1
            return
        self.current_tag = tag
        if tag in {"p", "div", "article", "section", "br", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "canvas", "iframe"}:
            self.skip_depth = max(self.skip_depth - 1, 0)
            return
        if tag in {"p", "div", "article", "section", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")
        self.current_tag = None

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        text = data.strip()
        if not text:
            return
        self.parts.append(text)
        if self.current_tag in {"p", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")
        else:
            self.parts.append(" ")

    def get_text(self) -> str:
        text = " ".join(" ".join(self.parts).split())
        text = re.sub(r"\s*\n\s*", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def date_to_gdelt_datetime(date_value: str, end_of_day: bool = False) -> str:
    compact = str(date_value).replace("-", "")
    return f"{compact}235959" if end_of_day else f"{compact}000000"


def article_doc_id(url: str) -> str:
    return stable_hash(f"gdelt_news|{url}")


def normalise_gdelt_time(value: str, fallback_date: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}T\d{6}Z", text):
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}T{text[9:11]}:{text[11:13]}:{text[13:15]}Z"
    if re.fullmatch(r"\d{14}", text):
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}T{text[8:10]}:{text[10:12]}:{text[12:14]}Z"
    if re.fullmatch(r"\d{8}", text):
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}T00:00:00Z"
    if "T" in text and "-" in text:
        return text
    return f"{fallback_date}T00:00:00Z"


def request_gdelt_json_with_limited_backoff(params: Dict[str, Any], timeout: Optional[int] = None) -> Dict[str, Any]:
    wait_seconds = float(getattr(config, "INITIAL_BACKOFF_SECONDS", 20))
    wait_cap = float(getattr(config, "MAX_BACKOFF_SECONDS", 60))
    timeout = timeout or min(int(getattr(config, "REQUEST_TIMEOUT", 30)), 30)

    for attempt in range(1, GDELT_ARTLIST_MAX_RETRIES + 1):
        try:
            response = requests.get(
                config.GDELT_DOC_API,
                params=params,
                timeout=timeout,
                headers={"User-Agent": "TransitPulse fast direct ES GDELT scraper"},
            )

            if response.status_code == 429:
                log.info(f"GDELT 429. Attempt {attempt}/{GDELT_ARTLIST_MAX_RETRIES}. Waiting {wait_seconds:g}s.")
                if attempt == GDELT_ARTLIST_MAX_RETRIES:
                    response.raise_for_status()
                time.sleep(wait_seconds)
                wait_seconds = min(wait_seconds * 2, wait_cap)
                continue

            if response.status_code in {500, 502, 503, 504}:
                log.info(f"GDELT {response.status_code}. Attempt {attempt}/{GDELT_ARTLIST_MAX_RETRIES}. Waiting {wait_seconds:g}s.")
                if attempt == GDELT_ARTLIST_MAX_RETRIES:
                    response.raise_for_status()
                time.sleep(wait_seconds)
                wait_seconds = min(wait_seconds * 2, wait_cap)
                continue

            response.raise_for_status()
            try:
                return response.json()
            except Exception as error:
                preview = (response.text or "")[:200].replace("\n", " ")
                raise RuntimeError(f"GDELT returned non-JSON response: {error}. Preview: {preview}") from error

        except requests.exceptions.RequestException as error:
            log.info(f"GDELT request failed. Attempt {attempt}/{GDELT_ARTLIST_MAX_RETRIES}. Error: {error}. Waiting {wait_seconds:g}s.")
            if attempt == GDELT_ARTLIST_MAX_RETRIES:
                raise
            time.sleep(wait_seconds)
            wait_seconds = min(wait_seconds * 2, wait_cap)

    raise RuntimeError("GDELT request failed after retry attempts.")


def fetch_article_fast(url: str) -> requests.Response:
    try:
        response = requests.get(
            url,
            timeout=ARTICLE_CONNECT_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 TransitPulse research scraper"},
        )
        if response.status_code in {403, 404, 410, 451}:
            raise RuntimeError(f"{response.status_code} permanent article HTTP error")
        response.raise_for_status()
        return response
    except requests.exceptions.RequestException as error:
        raise RuntimeError(str(error)) from error


def load_volume_rows(keyword: str, start: str, end: str) -> List[Dict[str, Any]]:
    body = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"keyword": keyword}},
                    {"range": {"date": {"gte": start, "lte": end}}},
                ]
            }
        },
        "sort": [{"date": "asc"}],
    }
    return search_all(config.VOLUME_INDEX, body, page_size=2000)


def allocate_targets(volume_rows: List[Dict[str, Any]], total_target: int, min_per_day: int) -> List[Tuple[str, int]]:
    positive = []
    for row in volume_rows:
        try:
            volume = float(row.get("volume") or 0)
        except Exception:
            volume = 0
        if volume > 0:
            positive.append((row.get("date"), volume))
    if not positive:
        return []
    total_volume = sum(volume for _, volume in positive)
    allocations = []
    for date_value, volume in positive:
        weighted = int(round(total_target * volume / total_volume))
        allocations.append((date_value, max(weighted, min_per_day)))
    return allocations


def fetch_gdelt_artlist(query, date_value, max_records):
    candidates, _ = discover(query, date_value)
    return candidates[:max_records]


def extract_article_text(html: str) -> str:
    parser = ArticleTextParser()
    parser.feed(html)
    return parser.get_text()


def scrape_article(candidate: Dict[str, Any], date_value: str) -> Dict[str, Any]:
    url = candidate["url"]
    response = fetch_article_fast(url)
    raw_html = response.text or ""
    text = extract_article_text(raw_html)
    if len(text) < config.MIN_TEXT_CHARS:
        raise RuntimeError(f"extracted_text_too_short:{len(text)}")
    return {
        "index_name": config.NEWS_INDEX,
        "id": article_doc_id(url),
        "time": normalise_gdelt_time(candidate.get("seendate"), date_value),
        "title": candidate.get("title") or "",
        "text": text,
        "raw_html": raw_html,
        "url": url,
        "source_domain": urlsplit(url).hostname,
        "source_query": config.GDELT_QUERY,
        "keyword": config.GDELT_KEYWORD,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "time_source": "gdelt_seendate",
        "dataset_kind": "live",
        "schema_version": 2,
    }


def news_doc_id(record: Dict[str, Any]) -> str:
    return record["id"]


def scrape_candidates_parallel(candidates: List[Dict[str, Any]], date_value: str, remaining_for_run: int, target_for_day: int) -> Tuple[List[Dict[str, Any]], int]:
    docs: List[Dict[str, Any]] = []
    failures = 0
    selected = []

    for candidate in candidates:
        if len(selected) >= max(remaining_for_run, 0) or len(selected) >= target_for_day:
            break
        doc_id = article_doc_id(candidate["url"])
        if doc_exists(config.NEWS_INDEX, doc_id):
            continue
        selected.append(candidate)

    if not selected:
        return [], 0

    workers = max(1, min(ARTICLE_WORKERS, len(selected)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(scrape_article, candidate, date_value): candidate for candidate in selected}
        for future in concurrent.futures.as_completed(future_map):
            candidate = future_map[future]
            try:
                docs.append(future.result())
            except Exception as error:
                failures += 1
                log.info(f"Skipping article because scrape failed: {candidate.get('url')} | {error}")
    return docs, failures


def run_scraper(start: str, end: str, keyword: str, query: str, target_articles: int, min_per_day: int, time_budget_minutes=None) -> Dict[str, Any]:
    if datetime.fromisoformat(start) > datetime.fromisoformat(end) or target_articles < 1 or min_per_day < 0:
        raise ValueError("Invalid date range or article budget")
    ensure_indexes()
    volume_rows = load_volume_rows(keyword, start, end)
    if not volume_rows:
        raise RuntimeError(f"No volume rows found in {config.VOLUME_INDEX}. Run 01_collect_volume.py first.")

    allocations = allocate_targets(volume_rows, target_articles, min_per_day)
    allocations = sorted(allocations, key=lambda item: item[1], reverse=True)

    deadline = time.time() + (time_budget_minutes or config.TIME_BUDGET_MINUTES) * 60
    scraped = 0
    uploaded = 0
    skipped_or_failed = 0
    skipped_dates = 0

    for date_value, target_for_day in allocations:
        if scraped >= target_articles:
            break
        if time.time() > deadline:
            log.info("Time budget reached. Stopping with partial result.")
            break

        log.info(f"Fetching GDELT ArtList for {date_value}, target={target_for_day}")
        safe_max_records = max(target_for_day * 3, 250)

        try:
            candidates = fetch_gdelt_artlist(query, date_value, max_records=safe_max_records)
        except Exception as error:
            skipped_dates += 1
            log.info(f"Skipping date because GDELT ArtList failed: {date_value} | {error}")
            time.sleep(config.GDELT_SLEEP_SECONDS)
            continue

        remaining = target_articles - scraped
        docs, failures = scrape_candidates_parallel(
            candidates=candidates,
            date_value=date_value,
            remaining_for_run=remaining,
            target_for_day=target_for_day,
        )
        skipped_or_failed += failures

        if docs:
            for doc in docs:
                doc["source_query"], doc["keyword"] = query, keyword
            uploaded += bulk_upsert(config.NEWS_INDEX, docs, news_doc_id)["created"]
            scraped += len(docs)
            log.info(f"Uploaded {len(docs)} articles for {date_value}. Total uploaded this run: {uploaded}")

        time.sleep(config.GDELT_SLEEP_SECONDS)

    result = {
        "status": "partial_success" if skipped_dates or skipped_or_failed or scraped < target_articles else "success",
        "index": config.NEWS_INDEX,
        "target_articles": target_articles,
        "articles_uploaded": uploaded,
        "articles_scraped_success": scraped,
        "articles_skipped_or_failed": skipped_or_failed,
        "dates_skipped_because_artlist_failed": skipped_dates,
    }
    log.info(json.dumps(result, indent=2, ensure_ascii=False))
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape GDELT news articles directly into Elasticsearch.")
    parser.add_argument("--start", default=config.GDELT_START_DATE)
    parser.add_argument("--end", default=config.GDELT_END_DATE or utc_today())
    parser.add_argument("--keyword", default=config.GDELT_KEYWORD)
    parser.add_argument("--query", default=config.GDELT_QUERY)
    parser.add_argument("--target-articles", type=int, default=config.TARGET_ARTICLES)
    parser.add_argument("--min-per-day", type=int, default=config.MIN_PER_DAY)
    parser.add_argument("--time-budget-minutes", type=int, default=config.TIME_BUDGET_MINUTES)
    return parser.parse_args()


def main(context=None) -> str:
    args = parse_args()
    result = run_scraper(args.start, args.end, args.keyword, args.query, args.target_articles, args.min_per_day, args.time_budget_minutes)
    return json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    print(main())
