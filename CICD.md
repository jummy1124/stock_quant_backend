# CI/CD — stock_quant_userdata

與 `stock_market`、`stock_quant_frontend` 同一套做法:**GitHub Actions + Workload Identity
Federation(無金鑰)+ Artifact Registry + GCP VM(IAP 隧道)**。差別在本服務是 Python/FastAPI,
測試用 pytest,且部署時多帶一個 PostgreSQL 容器。

## 流程總覽

```
push 任一分支 ─► CI: pytest (py3.11 + Poetry)
                     │
              push 到 main 且測試過 ─► build image ─► 推 Artifact Registry (tag: <sha> + latest)
                                                              │
每日 08:40(台北)/ 手動 ─► Deploy: 比對 deployed tag,有新版才部署
                                   └► scp 部署資產到 VM ─► IAP SSH 跑 deploy_on_vm.sh
                                        └► docker pull ─► compose up(postgres 常駐 + userdata 換版,
                                             entrypoint 跑 alembic 遷移)─► /health 檢查 ─► 失敗回滾
                                        └► 成功則把 `deployed` tag 指向本版(線上真相 / 回滾基準)
```

兩個 workflow:

- `.github/workflows/ci.yml` — 測試(所有分支)+ build & push image(僅 main)。
- `.github/workflows/deploy.yml` — 每日 / 手動部署到 VM(`workflow_dispatch` 可 `force=true` 強制重部署)。

設計重點(與其他兩個 repo 一致):

- **無金鑰**:用 WIF 換 GCP 短期憑證,GitHub 不存任何 GCP 金鑰(連 Secret 都不需要,全用 Variables)。
- **image 以 commit SHA 為不可變 tag**,部署精準、可回滾;`deployed` tag 標記目前線上版本。
- **健康檢查 + 自動回滾**:新版 `/health` 不過就回滾到上一版的 userdata image(postgres 與資料不動)。

## 1. 一次性 GCP 建置

在 Cloud Shell 用環境變數覆蓋後執行 `deploy/setup_gcp.sh`(會建 AR repo、部署 SA、WIF、VM 授權):

```bash
PROJECT_ID=my-project REGION=asia-east1 \
GITHUB_OWNER=jummy1124 GITHUB_REPO=stock_quant_backend \
VM_NAME=stock-quant-vm VM_ZONE=asia-east1-b \
VM_APP_DIR=/opt/stock-quant-userdata \
bash deploy/setup_gcp.sh
```

> 若同一 GCP 專案已為 `stock_market` / 前端跑過此類腳本,WIF pool、provider、部署 SA 可**共用**
> (腳本對已存在資源會略過);本服務只需新增自己的 Artifact Registry repo 與本 repo 的 WIF 綁定。

## 2. GitHub Repository Variables

到 repo → **Settings → Secrets and variables → Actions → Variables**,新增(非機密,用 Variables 即可;
WIF 不需任何 Secret)。`setup_gcp.sh` 跑完會直接印出對應值:

| Variable | 範例 |
| --- | --- |
| `GCP_PROJECT_ID` | `my-project` |
| `GCP_REGION` | `asia-east1` |
| `AR_REPOSITORY` | `stock-quant-userdata` |
| `IMAGE_NAME` | `stock-quant-userdata` |
| `GCP_WIF_PROVIDER` | `projects/123456789/locations/global/workloadIdentityPools/github-pool/providers/github-provider` |
| `GCP_DEPLOY_SA` | `gha-deployer@my-project.iam.gserviceaccount.com` |
| `GCE_VM_NAME` | `stock-quant-vm` |
| `GCE_VM_ZONE` | `asia-east1-b` |
| `VM_APP_DIR` | `/opt/stock-quant-userdata` |

## 3. VM 前置準備(一次性)

1. 安裝 Docker + Compose v2。
2. 建立 `${VM_APP_DIR}`,放入 **`.env`**(機密,不進 git)。最少需要:

   ```dotenv
   JWT_SECRET=<隨機長字串>
   JWT_EXPIRE_MINUTES=1440
   ALLOWED_ORIGINS=*
   APP_PORT=8100
   POSTGRES_USER=user
   POSTGRES_PASSWORD=<強密碼>
   POSTGRES_DB=userdata
   ```

   > 容器內的 `DATABASE_URL` 由 `docker-compose.deploy.yml` 自動指向 `postgres` 服務,**不需**寫在 `.env`
   > (即使寫了也會被 compose 的 `environment` 覆蓋)。

3. 前端 nginx 加反代(正式環境同源、免 CORS):

   ```nginx
   location /userapi/ { proxy_pass http://host.docker.internal:8100; }
   ```

部署資產(`deploy/docker-compose.deploy.yml`、`deploy/deploy_on_vm.sh`)由 deploy workflow 在每次部署時
scp 到 VM,不需手動放;`.env` 與 Postgres 的 named volume 則常駐 VM。

## 4. 手動部署 / 回滾

- **手動觸發**:GitHub → Actions → Daily Deploy → Run workflow(可勾 `force` 強制重部署)。
- **回滾**:deploy_on_vm.sh 在健康檢查失敗時會自動回滾到上一版 userdata image。要手動回滾某個舊版,
  可在 Actions 對該舊 commit 重跑、或在 VM 上 `IMAGE=<舊 sha 的完整 ref> bash deploy_on_vm.sh`。
- **資料安全**:重部署只重啟 `userdata` 容器,`postgres` 與 `userdata-pgdata` volume 不受影響;
  資料庫 schema 由 image entrypoint 的 `alembic upgrade head` 維護。

> ⚠️ 遷移相容性:自動回滾只換 userdata image,不會自動 `alembic downgrade`。若某版含破壞性 migration,
> 回滾舊 image 可能與已升級的 schema 不相容,需人工處理(此服務 schema 簡單,一般情況安全)。

## 5. 疑難排解

| 症狀 | 排查 |
| --- | --- |
| WIF 認證失敗 | 檢查 `GCP_WIF_PROVIDER` / `GCP_DEPLOY_SA` 變數、provider 的 `repository_owner` 條件、SA 的 workloadIdentityUser 綁定 |
| deploy 找不到 image | 確認該 commit 的 CI `build-and-push` 有成功(只有 push 到 main 才會 build) |
| VM 健康檢查逾時 | SSH 到 VM 看 `docker logs stock-quant-userdata`;常見為 `.env` 缺 `JWT_SECRET` 或 Postgres 未就緒 |
| Postgres 連線失敗 | 確認 `.env` 的 `POSTGRES_*` 與 compose 一致;首次啟動 volume 初始化需數秒,健康檢查已含重試 |
| pull image 被拒 | VM 附掛 SA 需有 `roles/artifactregistry.reader`(見 setup_gcp.sh 步驟 7) |
