# GitHub Actions 設定說明

這個版本不依賴 Codex App，也不需要你的 Mac 持續開機。
它會在 GitHub Actions 裡直接讀 Apple 頁面與店取資料，不需要你的 Mac 持續開機。

## 需要的檔案

- `.github/workflows/mac-mini-stock.yml`
- `github_actions_mac_mini_monitor.py`
- `check_mac_mini_stock.py`

## 需要設定的 Variables 與 Secrets

在 GitHub 倉庫的 `Settings` -> `Secrets and variables` -> `Actions` 裡新增：

Variables：

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `EMAIL_FROM`
- `EMAIL_TO`

Secrets：

- `SMTP_PASSWORD`

## 如果你要用 Google Workspace / Gmail SMTP

常見設定是：

- `SMTP_HOST = smtp.gmail.com`
- `SMTP_PORT = 465`
- `SMTP_USERNAME = 你的完整信箱`
- `SMTP_PASSWORD = app password`
- `EMAIL_FROM = 你的完整信箱`
- `EMAIL_TO = justin@g-mail.nsysu.edu.tw`

## 部署步驟

1. 把這個資料夾放進一個 GitHub 倉庫
2. 推到預設分支
3. 在 GitHub 開好上面的 Secrets
4. 到 `Actions` 頁面手動執行一次 `Mac mini 現貨監控`
5. 確認信件正常後，之後就會每 6 小時自動跑一次

## 驗證指令

本機只看輸出、不寄信：

```bash
uv run python github_actions_mac_mini_monitor.py --json --dry-run
uv run python github_actions_mac_mini_monitor.py --dry-run
```
