# 篩選快照儲存 + 下載 (Snapshots & Download)

把每日篩選結果存進資料庫，並提供一個與主前端分離的下載頁。三類資料：

1. **盤中 13:00 篩選** — `session = intraday_1300`
2. **收盤後篩選** — `session = eod`
3. **使用者自己的個股紀錄** — 既有的 `records` 表（每人私有）

## 資料流

```
stock_market/run_intraday.py  --ingest
        │  每交易日 13:00 一次 (intraday_1300) + 收盤後一次 (eod)
        │  POST /userapi/ingest/snapshot   header: X-Ingest-Token
        ▼
stock_quant_backend (FastAPI + Postgres)
        screen_snapshots / screen_snapshot_items 兩張表 (依 trade_date+session upsert)
        │
        ├── GET /downloadapi/snapshots            (公開) 列出可下載的快照
        ├── GET /downloadapi/snapshot.xlsx        (公開) 下載某日某 session 的 .xlsx
        └── GET /downloadapi/records.xlsx         (需 JWT) 下載自己的紀錄 .xlsx
        ▼
前端 download.html  (獨立頁，src/download/*；只依賴 /downloadapi + /userapi)
```

## 資料模型 (`app/models.py`)

- `ScreenSnapshot`：一筆 = 某 `trade_date` + `session` 的篩選快照頭（含 universe/quotable/pool_size/warning 等覆蓋度資訊）。`(trade_date, session)` 唯一。
- `ScreenSnapshotItem`：該快照通過 6 條件的起漲個股列（代號、名稱、市場、現價、漲幅、量、昨高、量比、5MA、20MA、月線上彎…）。

重新 ingest 同一 `(trade_date, session)` 會**整批覆蓋** items（idempotent），所以 screener 重試或重啟都安全。

## 端點

### 寫入（服務對服務）

`POST /userapi/ingest/snapshot`，header `X-Ingest-Token: <INGEST_TOKEN>`。
未設定 `INGEST_TOKEN` 時回 503（fail closed）；token 不符回 401。body 見 `schemas.SnapshotIngestBody`。

### 下載（給下載頁）

| 端點 | 權限 | 說明 |
|---|---|---|
| `GET /downloadapi/snapshots?limit=365` | 公開 | 最新在前的快照清單（只有表頭，不含個股列） |
| `GET /downloadapi/snapshot.xlsx?date=YYYY-MM-DD&session=intraday_1300\|eod` | 公開 | 下載該快照 .xlsx；查無資料回 200 + 一份標示「今日無符合」的空表 |
| `GET /downloadapi/records.xlsx` | 需 JWT | 下載登入者自己的個股紀錄 .xlsx |

> 篩選快照是全市場共用的參考資料 → 公開；個人紀錄 → 需登入。

## 設定

後端 `.env`（見 `.env.example`）：

```
INGEST_TOKEN=一段隨機長字串    # 與 screener 端相同；空字串=停用 ingest
```

screener 端 `stock_market/.env`：

```
INGEST_URL=http://localhost:8100
INGEST_TOKEN=與後端相同的隨機長字串
```

啟動 screener 並開啟上傳：

```bash
cd stock_market
python run_intraday.py --ingest                 # 盤中 13:00 + 收盤後自動上傳
python run_intraday.py --ingest --ingest-time 13:00
```

## 部署 / 路由

- 開發：`vite.config.ts` 已把 `/downloadapi` 代理到 userdata 後端（預設 8100）。
- 正式：`nginx.conf` 已加 `location /downloadapi/` 反代到 8100；下載頁是 `download.html`，與 `/downloadapi/` 不衝突。
- 多頁建置：`vite.config.ts` 的 `build.rollupOptions.input` 同時輸出 `index.html` 與 `download.html`。

## 日後拆成獨立專案

下載功能刻意自我內聚：後端 `app/routers/download.py` + `app/download_xlsx.py`（xlsx 產生器以 `TYPE_CHECKING` 引入 ORM 型別，不綁死 app），前端 `src/download/*` + `download.html`（不 import 主 App）。要拆出去時，搬這幾個檔到新專案、保持相同 `/downloadapi` 契約即可。

## 資料庫遷移

```bash
alembic upgrade head      # 套用 0002_screen_snapshots
```
