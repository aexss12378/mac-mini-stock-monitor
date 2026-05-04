#!/usr/bin/env python3
"""
GitHub Actions 專用的 Mac mini 現貨監控。

特點：
- 不依賴 Codex、Safari、Chrome 或 Apple Events
- 直接抓 Apple 台灣教育優惠頁與 fulfillment-messages
- 只要當下有符合條件的現貨就寄信，不做去重
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from check_mac_mini_stock import EDU_URL, REFURB_URL, USER_AGENT, check_education, check_refurbished, matches_target_variant


RETAIL_PICKUP_URL = "https://www.apple.com/tw/shop/retail/pickup-message"
DEFAULT_EMAIL_TO = "justin@g-mail.nsysu.edu.tw"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
MODEL_LINK_PREFIX = "/tw-edu/shop/buy-mac/mac-mini/"
DEFAULT_PICKUP_LOCATION = "110"
APPLE_TW_STORE_NAMES = {
    "R694": "Apple 信義 A13",
    "R713": "Apple 台北 101",
}
METRICS_PATTERN = re.compile(
    r'<script type="application/json" id="metrics">(.*?)</script>',
    re.DOTALL,
)
WARM_STATE_PATTERN = re.compile(
    r"window\.pageLevelData\.warmStateBootstrap = (\{.*?\});",
    re.DOTALL,
)


class AnchorExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.current_href: str | None = None
        self.current_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attributes = dict(attrs)
        href = attributes.get("href")
        if not href:
            return
        self.current_href = href.strip()
        self.current_text_parts = []

    def handle_data(self, data: str) -> None:
        if self.current_href is None:
            return
        self.current_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self.current_href is None:
            return
        text = normalize_text("".join(self.current_text_parts))
        self.links.append((self.current_href, text))
        self.current_href = None
        self.current_text_parts = []


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value).replace("\xa0", " ")).strip()


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="ignore")


def fetch_json(url: str, params: dict[str, str]) -> dict[str, Any]:
    full_url = f"{url}?{urlencode(params)}"
    request = Request(
        full_url,
        headers={
            "Accept": "application/json",
            "Referer": EDU_URL,
            "User-Agent": USER_AGENT,
        },
    )
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset, errors="ignore"))


def format_currency(value: float | int | None) -> str | None:
    if value is None:
        return None
    amount = int(round(float(value)))
    return f"NT${amount:,}"


def strip_markup(value: str | None) -> str:
    if not value:
        return ""
    without_tags = re.sub(r"<[^>]+>", "", value)
    return normalize_text(without_tags)





def extract_education_model_links(page_html: str) -> list[dict[str, str]]:
    parser = AnchorExtractor()
    parser.feed(page_html)
    seen: set[str] = set()
    models: list[dict[str, str]] = []
    for href, text in parser.links:
        normalized_href = href.strip()
        if not (
            normalized_href.startswith(MODEL_LINK_PREFIX)
            or normalized_href.startswith(f"https://www.apple.com{MODEL_LINK_PREFIX}")
        ):
            continue
        if not text.startswith("Mac mini，"):
            continue
        if not matches_target_variant(text):
            continue
        absolute_url = urljoin(EDU_URL, normalized_href)
        if absolute_url in seen:
            continue
        seen.add(absolute_url)
        models.append({"model": text, "url": absolute_url})
    return models


def extract_metrics_data(page_html: str) -> dict[str, Any]:
    match = METRICS_PATTERN.search(page_html)
    if not match:
        raise ValueError("找不到 metrics JSON。")
    return json.loads(match.group(1))


def extract_fulfillment_config(page_html: str) -> dict[str, Any]:
    match = WARM_STATE_PATTERN.search(page_html)
    if not match:
        raise ValueError("找不到 warmStateBootstrap。")

    warm_state = json.loads(match.group(1))
    default_kit = warm_state.get("defaultKit") or {}
    options = default_kit.get("options") or {}
    option_codes = [value for value in options.values() if isinstance(value, str) and value]

    if not default_kit.get("part"):
        raise ValueError("warmStateBootstrap 缺少 defaultKit.part。")

    return {
        "part": default_kit["part"],
        "option_codes": option_codes,
    }


def collect_pickup_entries(part_number: str) -> list[dict[str, str]]:
    payload = fetch_json(RETAIL_PICKUP_URL, {
        "parts.0": part_number,
        "location": DEFAULT_PICKUP_LOCATION,
        "searchNearby": "true",
    })
    stores = payload.get("body", {}).get("stores") or []
    entries: list[dict[str, str]] = []
    for store in stores:
        store_number = store.get("storeNumber", "")
        if store_number not in APPLE_TW_STORE_NAMES:
            continue
        part_avail = (store.get("partsAvailability") or {}).get(part_number, {})
        if part_avail.get("pickupDisplay") != "available":
            continue
        pickup_quote = (
            strip_markup(part_avail.get("storePickupQuote"))
            or strip_markup(part_avail.get("pickupSearchQuote"))
            or "可取貨"
        )
        entries.append({
            "store_id": store_number,
            "store": APPLE_TW_STORE_NAMES[store_number],
            "eligible": "true",
            "pickup": pickup_quote,
        })
    return entries


def collect_education_models() -> list[dict[str, Any]]:
    page_html = fetch_text(EDU_URL)
    model_links = extract_education_model_links(page_html)
    models: list[dict[str, Any]] = []

    for model_link in model_links:
        model_html = fetch_text(model_link["url"])
        metrics = extract_metrics_data(model_html)
        fulfillment_config = extract_fulfillment_config(model_html)
        product = metrics["data"]["products"][0]
        part_number = product["partNumber"]
        pickup_entries: list[dict[str, str]] = []
        pickup_error = None

        try:
            pickup_entries = collect_pickup_entries(part_number)
        except Exception as exc:
            pickup_error = f"Apple 直營店取貨：目前無法取得資料（{exc}）"

        models.append(
            {
                "model": model_link["model"],
                "url": model_link["url"],
                "part_number": part_number,
                "fulfillment_part": fulfillment_config["part"],
                "option_codes": fulfillment_config["option_codes"],
                "price": format_currency(product.get("price", {}).get("fullPrice")),
                "pickup_entries": pickup_entries,
                "pickup_error": pickup_error,
            }
        )

    return models


def build_email_body(refurbished: dict[str, Any], education_models: list[dict[str, Any]]) -> str:
    lines = [
        "Mac mini 現貨通知",
        "",
        f"檢查時間：{datetime.now(TAIPEI_TZ).strftime('%Y-%m-%d %H:%M:%S')}（台北時間）",
        "",
        "來源網址：",
        f"- Apple 台灣整修品：{REFURB_URL}",
        f"- Apple 台灣教育優惠：{EDU_URL}",
        "",
        "Apple 台灣整修品：",
    ]

    if refurbished["available"]:
        for detail in refurbished.get("details", []):
            lines.append(f"- {detail}")
    else:
        lines.append("- 目前沒有符合條件的現貨")

    lines.extend(["", "Apple 台灣教育優惠："])

    if not education_models:
        lines.append("- 目前沒有符合條件的現貨")
        return "\n".join(lines)

    for model in education_models:
        lines.append(f"- {model['model']}")
        if model.get("price"):
            lines.append(f"  售價：{model['price']}")
        lines.append(f"  商品頁：{model['url']}")
        if model.get("pickup_entries"):
            for pickup_entry in model["pickup_entries"]:
                lines.append(f"  {pickup_entry['store']}：{pickup_entry['pickup']}")
        elif model.get("pickup_error"):
            lines.append(f"  {model['pickup_error']}")
        else:
            lines.append("  Apple 直營店取貨：目前無法取得資料")
        lines.append("")

    return "\n".join(lines).rstrip()


def build_payload(refurbished: dict[str, Any], education_models: list[dict[str, Any]], edu_available: bool) -> dict[str, Any]:
    return {
        "checked_at": datetime.now(TAIPEI_TZ).isoformat(timespec="seconds"),
        "available_now": refurbished["available"] or edu_available,
        "refurbished": refurbished,
        "education_models": education_models,
    }


def send_email(subject: str, body: str) -> None:
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    smtp_port = int(os.environ.get("SMTP_PORT", "465").strip())
    smtp_username = os.environ.get("SMTP_USERNAME", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "").strip()
    email_from = os.environ.get("EMAIL_FROM", "").strip() or smtp_username
    email_to = os.environ.get("EMAIL_TO", "").strip() or DEFAULT_EMAIL_TO

    missing = [
        name
        for name, value in [
            ("SMTP_HOST", smtp_host),
            ("SMTP_USERNAME", smtp_username),
            ("SMTP_PASSWORD", smtp_password),
        ]
        if not value
    ]
    if not email_from:
        missing.append("EMAIL_FROM")
    if missing:
        raise RuntimeError(f"缺少必要環境變數：{', '.join(missing)}")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = email_from
    message["To"] = email_to
    message.set_content(body)

    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as server:
            server.login(smtp_username, smtp_password)
            server.send_message(message)
        return

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(smtp_username, smtp_password)
        server.send_message(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只輸出結果，不寄信",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="輸出 JSON",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        refurbished = check_refurbished()
    except (URLError, OSError) as exc:
        print(f"抓取整修品頁面失敗：{exc}", file=sys.stderr)
        return 1

    try:
        edu_check = check_education()
    except (URLError, OSError) as exc:
        print(f"抓取教育優惠頁面失敗：{exc}", file=sys.stderr)
        return 1

    try:
        education_models = collect_education_models() if edu_check["available"] else []
    except (URLError, OSError) as exc:
        print(f"抓取教育優惠商品頁失敗：{exc}", file=sys.stderr)
        return 1

    payload = build_payload(refurbished, education_models, edu_check["available"])

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(build_email_body(refurbished, education_models))

    if args.dry_run:
        return 0

    if not payload["available_now"]:
        print("目前沒有符合條件的現貨。")
        return 0

    send_email("Mac mini 現貨通知", build_email_body(refurbished, education_models))
    print("通知信已寄出。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - GitHub Actions 失敗時需要清楚錯誤
        print(f"執行失敗：{exc}", file=sys.stderr)
        raise
