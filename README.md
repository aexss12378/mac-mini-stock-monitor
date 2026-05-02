# Apple 台灣 Mac mini 現貨監控

這個專案用 GitHub Actions 定期檢查 Apple 台灣 Mac mini 供應狀態。符合條件時，工作流程會用 SMTP 寄出通知信。

只要有 GitHub 帳號即可使用，不需要在自己電腦上安裝或持續執行任何東西。

## 目前監控範圍

- Apple 台灣整修品頁：`https://www.apple.com/tw/shop/refurbished/mac`
- Apple 台灣教育優惠 Mac mini：`https://www.apple.com/tw-edu/shop/buy-mac/mac-mini`
- 目標記憶體：`16GB`、`24GB`
- 排除：`M4 Pro`
- 通知內容：型號、售價、商品頁、Apple 直營店取貨資訊
- 目前門市名稱對照：Apple 台北 101、Apple 信義 A13

整修品頁有商品不代表一定會通知。程式只把符合目標條件的 Mac mini 整修品算成現貨；如果頁面上只有 iMac、MacBook 或其他不符合條件的商品，通知裡會顯示沒有符合條件的整修品現貨。

## 專案檔案

- `.github/workflows/mac-mini-stock.yml`：GitHub Actions 排程與執行入口
- `github_actions_mac_mini_monitor.py`：正式寄信入口，供 GitHub Actions 使用
- `check_mac_mini_stock.py`：共用的 Apple 頁面抓取與目標型號判斷
- `.gitignore`：避免把快取、狀態檔、瀏覽器紀錄推上 GitHub

## 安裝方式

1. Fork 這個儲存庫到自己的 GitHub 帳號。
2. 到 GitHub 儲存庫的 `Settings` -> `Secrets and variables` -> `Actions` 設定寄信資訊。

Variables：

```text
SMTP_HOST
SMTP_PORT
SMTP_USERNAME
EMAIL_FROM
EMAIL_TO
```

Secrets：

```text
SMTP_PASSWORD
```

Gmail SMTP 常見格式：

```text
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USERNAME=寄件信箱
SMTP_PASSWORD=Gmail 應用程式密碼
EMAIL_FROM=寄件信箱
EMAIL_TO=收件信箱
```

## 使用方式

GitHub Actions 會每 6 小時執行一次，也可以手動觸發：

1. 到 GitHub repository 的 `Actions` 頁面。
2. 選擇 `Mac mini 現貨監控`。
3. 點選 `Run workflow`。
4. 等待工作流程完成，檢查日誌和收件信箱。

本機執行需要 Python 3.12 與 [uv](https://github.com/astral-sh/uv)。

只看結果、不寄信：

```bash
uv run python github_actions_mac_mini_monitor.py --json --dry-run
uv run python github_actions_mac_mini_monitor.py --dry-run
```

只檢查基本現貨判斷：

```bash
uv run python check_mac_mini_stock.py --json
```

## 自訂設定

### 改監控 URL

整修品頁和教育優惠頁的網址定義在 `check_mac_mini_stock.py`：

```python
REFURB_URL = "https://www.apple.com/tw/shop/refurbished/mac"
EDU_URL = "https://www.apple.com/tw-edu/shop/buy-mac/mac-mini"
```

若要監控其他商品，改 `EDU_URL` 指向目標商品頁。注意 Apple 前端常把 `partNumber`、價格等關鍵資料放在頁面內嵌 JSON，而不是純文字，改 URL 後要確認解析邏輯仍能取到正確欄位。

### 改監控型號條件

調整 `check_mac_mini_stock.py`：

- `TARGET_MEMORY_OPTIONS`：符合條件的記憶體規格，例如 `("16GB", "24GB")`
- `EXCLUDED_CHIP_PHRASES`：排除特定晶片，例如 `("M4 Pro",)`
- `matches_target_variant()`：需要比對 CPU、GPU、容量等更細條件時改這裡

### 改排程

調整 `.github/workflows/mac-mini-stock.yml` 的 `cron`。目前是每 6 小時一次（`0 */6 * * *`）。

Cron 語法：`分 時 日 月 週`，例如每小時執行改成 `0 * * * *`。

### 改門市對照與取貨地點

`github_actions_mac_mini_monitor.py` 有兩個相關設定：

```python
DEFAULT_PICKUP_LOCATION = "110"  # 台灣郵遞區號，影響取貨查詢範圍

APPLE_TW_STORE_NAMES = {
    "R694": "Apple 信義 A13",
    "R713": "Apple 台北 101",
}
```

若 Apple 台灣新增門市，從 Apple 頁面原始碼或 API 回應找到門市代號（格式如 `R###`），補進 `APPLE_TW_STORE_NAMES`。若要監控其他地區，一併調整 `DEFAULT_PICKUP_LOCATION` 和 `APPLE_TW_EDU_COOKIE` 裡的商店脈絡。

## 疑難排解

### 整修品頁明明有商品，為什麼通知說沒有？

程式只通知符合條件的 Mac mini。整修品頁有 iMac、MacBook 或其他 Mac，不代表有符合條件的 Mac mini。

### Apple 直營店取貨資料空白

先看 GitHub Actions 日誌。如果教育優惠商品和價格都有出現，但店取資料空白，常見原因是 Apple 修改取貨接口、地點資訊失效，或新增了不同的門市回傳格式。

### 沒收到信

確認 GitHub Actions 裡有設定：

```text
SMTP_HOST
SMTP_PORT
SMTP_USERNAME
SMTP_PASSWORD
EMAIL_FROM
EMAIL_TO
```

再看 workflow 日誌最後是否有出現「通知信已寄出」。如果沒有，先修 SMTP 設定；如果有，檢查垃圾信、收件規則或寄件帳號安全設定。

### 工作流程失敗

到 GitHub `Actions` 頁面，打開失敗的 run，先看 `執行監控` 這一步。常見原因是 Apple 頁面結構改變、Apple 暫時拒絕請求、SMTP 設定缺漏，或 GitHub Actions 暫時性網路問題。

## 安全注意事項

- 密碼、應用程式密碼、token 只放在 GitHub Actions Secrets。
- 一般環境變數放在 GitHub Actions Variables。
- 不要把實際寄件信箱、收件信箱、密碼、cookie 或 token 寫進公開文件、程式註解或 commit 訊息。

## 問題解決紀錄

### 原始問題

自己用瀏覽器開 Apple 網頁，可以看到哪間門市有現貨、可以取貨。但同樣的程式跑在 GitHub Actions 上，卻顯示「目前無法取得資料」或完全沒有門市資訊。

### 原因

Apple 的門市取貨資訊不是直接寫在頁面上，而是瀏覽器在背後自動帶著「我在台灣、我要查這個商品、我在這個郵遞區號附近」這些條件去問 Apple 伺服器才拿到的。瀏覽器會自動處理這些，但 GitHub Actions 跑在雲端伺服器上，什麼都沒有，Apple 就不回傳門市資料。

舊版程式查的是一個叫 `fulfillment-messages` 的 API，這個在瀏覽器裡可以通，在雲端就行不通。

### 修法

改成讓程式自己帶齊 Apple 需要的資訊：

1. 從教育優惠頁抓出符合條件的商品連結。
2. 進到商品頁，取得這個商品的實際料號（`partNumber`）。
3. 用料號去問另一個 API（`sba/availability-message`），這個不需要瀏覽器狀態。
4. 查詢時帶入台灣的地區代碼和郵遞區號。
5. 再對幾個已知的 Apple 台灣門市代號做備援查詢。

改完之後，GitHub Actions 就能正確列出 Apple 台北 101、Apple 信義 A13 的取貨狀況、售價與供貨日期。

### 通知邏輯

每次執行只看「現在」有沒有貨，不和上次的結果比較。只要這次跑出來有符合條件的現貨，就寄通知。
