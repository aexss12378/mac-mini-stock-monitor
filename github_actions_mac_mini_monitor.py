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
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from check_mac_mini_stock import EDU_URL, REFURB_URL, USER_AGENT, check_refurbished, matches_target_variant

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - 讓本機未安裝時有清楚錯誤
    sync_playwright = None


FULFILLMENT_URL = "https://www.apple.com/tw-edu/shop/fulfillment-messages"
DEFAULT_EMAIL_TO = "justin@g-mail.nsysu.edu.tw"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
MODEL_LINK_PREFIX = "/tw-edu/shop/buy-mac/mac-mini/"
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


def format_currency(value: float | int | None) -> str | None:
    if value is None:
        return None
    amount = int(round(float(value)))
    return f"NT${amount:,}"


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


class PickupBrowser:
    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._page = None

    def __enter__(self) -> "PickupBrowser":
        if sync_playwright is None:
            raise RuntimeError(
                "缺少 playwright。請用 `uv run --with playwright python github_actions_mac_mini_monitor.py` 執行。"
            )

        self._playwright = sync_playwright().start()
        launch_errors: list[str] = []
        channel = os.environ.get("PLAYWRIGHT_CHROME_CHANNEL", "chrome").strip()

        if channel:
            try:
                self._browser = self._playwright.chromium.launch(channel=channel, headless=True)
            except Exception as exc:  # pragma: no cover - 視執行環境而定
                launch_errors.append(f"{channel}: {exc}")

        if self._browser is None:
            try:
                self._browser = self._playwright.chromium.launch(headless=True)
            except Exception as exc:  # pragma: no cover - 視執行環境而定
                launch_errors.append(f"chromium: {exc}")
                message = "；".join(launch_errors) or "無法啟動瀏覽器"
                raise RuntimeError(f"無法啟動無頭瀏覽器：{message}") from exc

        self._page = self._browser.new_page()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    def collect_pickup_entries(
        self,
        model_url: str,
        availability_part_number: str,
        fulfillment_part: str,
        option_codes: list[str],
    ) -> list[dict[str, str]]:
        assert self._page is not None
        self._page.goto(model_url, wait_until="domcontentloaded")
        entries = self._page.evaluate(
            """
            async ({ availabilityPartNumber, fulfillmentPart, fulfillmentUrl, optionCodes }) => {
              const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const buildUrl = (storeId) => {
                const url = new URL(fulfillmentUrl);
                url.searchParams.set("fae", "true");
                url.searchParams.set("little", "false");
                url.searchParams.set("parts.0", fulfillmentPart);
                if (optionCodes.length) {
                  url.searchParams.set("option.0", optionCodes.join(","));
                }
                url.searchParams.set("mts.0", "regular");
                url.searchParams.set("mts.1", "sticky");
                url.searchParams.set("mts.2", "compact");
                url.searchParams.set("fts", "true");
                if (storeId) {
                  url.searchParams.set("store", storeId);
                }
                return url.toString();
              };

              const fetchPickupMessage = async (storeId) => {
                const response = await fetch(buildUrl(storeId));
                if (!response.ok) {
                  throw new Error(`fulfillment ${response.status}`);
                }
                const payload = await response.json();
                return payload.body.content.pickupMessage;
              };

              const toEntry = (pickupMessage, storeId) => {
                const stores = pickupMessage.stores || [];
                if (!stores.length) {
                  return null;
                }
                const store = stores[0];
                const availabilityMap = store.partsAvailability || {};
                let availability = availabilityMap[availabilityPartNumber];
                if (!availability) {
                  const values = Object.values(availabilityMap);
                  if (values.length === 1) {
                    availability = values[0];
                  }
                }
                if (!availability) {
                  return null;
                }

                const name =
                  normalize(store?.retailStore?.address?.companyName) ||
                  normalize(store?.address?.address) ||
                  normalize(store?.storeName) ||
                  storeId ||
                  "Apple 直營店";

                const pickup =
                  normalize(availability?.messageTypes?.regular?.storePickupQuote) ||
                  normalize(availability?.pickupSearchQuote) ||
                  normalize(availability?.messageTypes?.sticky?.storePickupQuote) ||
                  "狀態未知";

                return {
                  store_id: storeId || store.storeNumber || "",
                  store: name,
                  eligible: availability.storePickEligible ? "true" : "false",
                  pickup
                };
              };

              const first = await fetchPickupMessage(null);
              const storeIds = normalize(first.availabilityStores)
                .split(",")
                .map((value) => value.trim())
                .filter(Boolean);

              const entries = [];
              const seen = new Set();

              if (storeIds.length) {
                for (const storeId of storeIds) {
                  const entry = toEntry(await fetchPickupMessage(storeId), storeId);
                  if (!entry) {
                    continue;
                  }
                  const key = `${entry.store_id}::${entry.pickup}`;
                  if (seen.has(key)) {
                    continue;
                  }
                  seen.add(key);
                  entries.push(entry);
                }
              }

              if (entries.length) {
                return entries;
              }

              const fallback = toEntry(first, "");
              return fallback ? [fallback] : [];
            }
            """,
            {
                "availabilityPartNumber": availability_part_number,
                "fulfillmentPart": fulfillment_part,
                "fulfillmentUrl": FULFILLMENT_URL,
                "optionCodes": option_codes,
            },
        )
        return entries


def collect_education_models() -> list[dict[str, Any]]:
    page_html = fetch_text(EDU_URL)
    model_links = extract_education_model_links(page_html)
    models: list[dict[str, Any]] = []

    with PickupBrowser() as pickup_browser:
        for model_link in model_links:
            model_html = fetch_text(model_link["url"])
            metrics = extract_metrics_data(model_html)
            fulfillment_config = extract_fulfillment_config(model_html)
            product = metrics["data"]["products"][0]
            part_number = product["partNumber"]
            pickup_entries: list[dict[str, str]] = []
            pickup_error = None

            try:
                pickup_entries = pickup_browser.collect_pickup_entries(
                    model_link["url"],
                    part_number,
                    fulfillment_config["part"],
                    fulfillment_config["option_codes"],
                )
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


def build_payload(refurbished: dict[str, Any], education_models: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "checked_at": datetime.now(TAIPEI_TZ).isoformat(timespec="seconds"),
        "available_now": refurbished["available"] or bool(education_models),
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
        education_models = collect_education_models()
    except (URLError, OSError) as exc:
        print(f"抓取教育優惠頁面失敗：{exc}", file=sys.stderr)
        return 1

    payload = build_payload(refurbished, education_models)

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
