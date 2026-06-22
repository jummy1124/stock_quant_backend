#!/usr/bin/env bash
# =============================================================================
# 一次性 GCP 建置：API / Artifact Registry / WIF / 部署 SA / IAM / VM 授權。
# 在 Cloud Shell 跑：先用環境變數覆蓋下方變數區 → bash deploy/setup_gcp.sh
#   → 把輸出的清單貼到 GitHub repo 的 Settings → Variables。
#
# ⚠️ 不要把真實 PROJECT_ID / VM / SA 寫死 commit 進 repo；用環境變數覆蓋執行。
#
# 若這個 GCP 專案已經為其他服務 (stock_market / 前端) 跑過本類腳本，
# WIF pool / provider / 部署 SA 可「共用」(腳本對已存在資源會印『已存在』略過)，
# 只需新增本服務專屬的 Artifact Registry repo 與本 repo 的 WIF 綁定即可。
# =============================================================================
set -euo pipefail

# ---- 變數區 (用環境變數覆蓋；repo 內只留 placeholder) ----
PROJECT_ID="${PROJECT_ID:-your-gcp-project-id}"
REGION="${REGION:-asia-east1}"                       # 要與 VM、AR 同區
AR_REPOSITORY="${AR_REPOSITORY:-stock-quant-userdata}"
IMAGE_NAME="${IMAGE_NAME:-stock-quant-userdata}"
GITHUB_OWNER="${GITHUB_OWNER:-your-github-account}"
GITHUB_REPO="${GITHUB_REPO:-stock_quant_backend}"
VM_NAME="${VM_NAME:-your-vm-name}"
VM_ZONE="${VM_ZONE:-asia-east1-b}"
DEPLOY_SA_NAME="${DEPLOY_SA_NAME:-gha-deployer}"     # 可與其他 repo 共用同一部署 SA
POOL_ID="${POOL_ID:-github-pool}"                    # 可與其他 repo 共用同一 WIF pool
PROVIDER_ID="${PROVIDER_ID:-github-provider}"
VM_APP_DIR="${VM_APP_DIR:-/opt/stock-quant-userdata}"
# ------------------------------------------------

DEPLOY_SA_EMAIL="${DEPLOY_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud config set project "${PROJECT_ID}"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"

echo "==> 1. 啟用 API"
gcloud services enable artifactregistry.googleapis.com iamcredentials.googleapis.com \
  iam.googleapis.com sts.googleapis.com compute.googleapis.com iap.googleapis.com

echo "==> 2. Artifact Registry (本服務專屬 repo)"
gcloud artifacts repositories create "${AR_REPOSITORY}" --repository-format=docker \
  --location="${REGION}" 2>/dev/null || echo "   (已存在)"

echo "==> 3. 部署 SA (可與其他 repo 共用)"
gcloud iam service-accounts create "${DEPLOY_SA_NAME}" \
  --display-name="GitHub Actions deployer" 2>/dev/null || echo "   (已存在)"

echo "==> 4. 授權部署 SA"
# 4a. 推 image 到本服務的 AR repo
gcloud artifacts repositories add-iam-policy-binding "${AR_REPOSITORY}" --location="${REGION}" \
  --member="serviceAccount:${DEPLOY_SA_EMAIL}" --role="roles/artifactregistry.writer" >/dev/null
# 4b. 透過 IAP 隧道 SSH/SCP 到 VM、操作 docker、解析 VM
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${DEPLOY_SA_EMAIL}" --role="roles/iap.tunnelResourceAccessor" --condition=None >/dev/null
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${DEPLOY_SA_EMAIL}" --role="roles/compute.osAdminLogin" --condition=None >/dev/null
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${DEPLOY_SA_EMAIL}" --role="roles/compute.viewer" --condition=None >/dev/null

echo "==> 5. Workload Identity Federation (無金鑰 OIDC，可與其他 repo 共用 pool/provider)"
gcloud iam workload-identity-pools create "${POOL_ID}" \
  --location="global" --display-name="GitHub Actions pool" 2>/dev/null || echo "   (pool 已存在)"
gcloud iam workload-identity-pools providers create-oidc "${PROVIDER_ID}" \
  --location="global" --workload-identity-pool="${POOL_ID}" \
  --display-name="GitHub provider" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition="assertion.repository_owner=='${GITHUB_OWNER}'" \
  2>/dev/null || echo "   (provider 已存在)"

echo "==> 6. 綁定：只有此 GitHub repo 可冒用部署 SA"
gcloud iam service-accounts add-iam-policy-binding "${DEPLOY_SA_EMAIL}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/${GITHUB_OWNER}/${GITHUB_REPO}" >/dev/null

echo "==> 7. 確保 VM 啟用 OS Login，且其附掛 SA 可讀本服務的 AR repo"
gcloud compute instances add-metadata "${VM_NAME}" --zone="${VM_ZONE}" \
  --metadata=enable-oslogin=TRUE 2>/dev/null || echo "   (略過：請確認 VM 存在且已啟用 OS Login)"
VM_SA="$(gcloud compute instances describe "${VM_NAME}" --zone="${VM_ZONE}" \
  --format='value(serviceAccounts[0].email)' 2>/dev/null || true)"
if [ -n "${VM_SA}" ]; then
  gcloud artifacts repositories add-iam-policy-binding "${AR_REPOSITORY}" \
    --location="${REGION}" \
    --member="serviceAccount:${VM_SA}" \
    --role="roles/artifactregistry.reader" >/dev/null
  echo "   VM 附掛 SA: ${VM_SA} 已獲 AR Reader"
else
  echo "   ⚠️ 找不到 VM 附掛 SA，請手動授予該 SA roles/artifactregistry.reader"
fi

WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}"

cat <<EOF

============================================================================
✅ 完成。請到 GitHub repo > Settings > Secrets and variables > Actions
   > Variables 新增以下 Repository variables：

  GCP_PROJECT_ID   = ${PROJECT_ID}
  GCP_REGION       = ${REGION}
  AR_REPOSITORY    = ${AR_REPOSITORY}
  IMAGE_NAME       = ${IMAGE_NAME}
  GCP_WIF_PROVIDER = ${WIF_PROVIDER}
  GCP_DEPLOY_SA    = ${DEPLOY_SA_EMAIL}
  GCE_VM_NAME      = ${VM_NAME}
  GCE_VM_ZONE      = ${VM_ZONE}
  VM_APP_DIR       = ${VM_APP_DIR}

VM 端前置 (一次性，見 CICD.md「VM 前置準備」)：
  - 安裝 Docker + Compose v2
  - 建立 ${VM_APP_DIR}，放入 .env (JWT_SECRET / POSTGRES_* 等密鑰)
  - 前端 nginx 需有 location /userapi/ 反代到 host.docker.internal:8100
============================================================================
EOF
