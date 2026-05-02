#!/usr/bin/env python3
"""
檢查 Apple 台灣 Mac mini 供應狀態，供外部自動化流程決定是否寄送通知。

目前模式只看「這一次」的結果：
- 只要當下有符合條件的型號，就視為應通知
- 不把這次結果和上次結果做去重綁定

執行範例：
    uv run python check_mac_mini_stock.py --json
"""

from __future__ import annotations

import argparse
import html
from html.parser import HTMLParser
import json
import re
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REFURB_URL = "https://www.apple.com/tw/shop/refurbished/mac"
EDU_URL = "https://www.apple.com/tw-edu/shop/buy-mac/mac-mini"
TARGET_MEMORY_OPTIONS = ("16GB", "24GB")
EXCLUDED_CHIP_PHRASES = ("M4 Pro",)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)
BLOCK_TAGS = {
    "article",
    "br",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "footer",
    "li",
    "main",
    "p",
    "section",
    "span",
    "tr",
}
IGNORED_TAGS = {"script", "style", "noscript"}
EDU_UNAVAILABLE_PHRASES = (
    "目前無法供應",
    "暫時無法供應",
    "售完",
    "無法送達",
)


class LineTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in IGNORED_TAGS:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in IGNORED_TAGS and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if self.ignored_depth:
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.ignored_depth:
            return
        self.parts.append(data)

    def get_lines(self) -> list[str]:
        text = html.unescape("".join(self.parts)).replace("\xa0", " ")
        lines = []
        for raw_line in text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if line:
                lines.append(line)
        return lines


def fetch_html(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="ignore")


def extract_lines(page_html: str) -> list[str]:
    parser = LineTextExtractor()
    parser.feed(page_html)
    return parser.get_lines()


def unique_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def matches_target_variant(line: str) -> bool:
    return (
        "Mac mini" in line
        and any(memory in line for memory in TARGET_MEMORY_OPTIONS)
        and not any(chip in line for chip in EXCLUDED_CHIP_PHRASES)
    )


def check_refurbished() -> dict[str, Any]:
    page_html = fetch_html(REFURB_URL)
    lines = extract_lines(page_html)
    product_lines = unique_preserving_order(
        [
            line
            for line in lines
            if matches_target_variant(line)
            and "整修品" in line
            and "關於 Mac 整修品" not in line
        ]
    )
    return {
        "name": "Apple 台灣整修品",
        "url": REFURB_URL,
        "available": bool(product_lines),
        "details": product_lines,
    }


def check_education() -> dict[str, Any]:
    page_html = fetch_html(EDU_URL)
    lines = extract_lines(page_html)
    joined = "\n".join(lines)
    has_buy_heading = "購買 Mac mini" in joined
    model_lines = unique_preserving_order(
        [line for line in lines if line.startswith("Mac mini，") and matches_target_variant(line)]
    )
    unavailable_hits = [phrase for phrase in EDU_UNAVAILABLE_PHRASES if phrase in joined]
    available = has_buy_heading and bool(model_lines) and not unavailable_hits
    return {
        "name": "Apple 台灣教育優惠",
        "url": EDU_URL,
        "available": available,
        "details": model_lines[:4],
        "warnings": unavailable_hits,
    }


def print_report(results: list[dict[str, Any]]) -> None:
    for result in results:
        status = "有現貨" if result["available"] else "沒有現貨"
        print(f"[{result['name']}] {status}")
        if result.get("details"):
            for detail in result["details"]:
                print(f"  - {detail}")
        if result.get("warnings"):
            for warning in result["warnings"]:
                print(f"  - 警示：{warning}")
        print(f"  - {result['url']}")


def build_payload(
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "available_now": any(result["available"] for result in results),
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只輸出結果",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="輸出 JSON 結果",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        results = [check_refurbished(), check_education()]
    except HTTPError as exc:
        print(f"抓取 Apple 頁面失敗：HTTP {exc.code}", file=sys.stderr)
        return 1
    except URLError as exc:
        print(f"抓取 Apple 頁面失敗：{exc.reason}", file=sys.stderr)
        return 1

    available_now = any(result["available"] for result in results)

    if args.json:
        print(
            json.dumps(
                build_payload(results),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print_report(results)

    if args.dry_run:
        return 0

    if args.json:
        return 0

    if not available_now:
        print("目前沒有符合條件的現貨。")
    else:
        print("目前有符合條件的現貨。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
