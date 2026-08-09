#!/usr/bin/env bash
set -euo pipefail

DEPLOY_PROJECT="${DEPLOY_PROJECT:-durable-stack-502219-q0}"
DEPLOY_REGION="${DEPLOY_REGION:-us-central1}"
INTERACTIVE_SERVICE="${INTERACTIVE_SERVICE:-google-ads-mcp}"
AUTOMATION_SERVICE="${AUTOMATION_SERVICE:-google-ads-mcp-automation}"
WORKER_JOB="${WORKER_JOB:-bmw-ads-recommendation-worker}"
ARTIFACT_REPOSITORY="${ARTIFACT_REPOSITORY:-constellation-automation}"
BMW_CUSTOMER_ID="4357201747"

MCP_SERVICE_ACCOUNT_NAME="google-ads-automation-mcp"
WORKER_SERVICE_ACCOUNT_NAME="bmw-ads-worker"
MCP_SERVICE_ACCOUNT="${MCP_SERVICE_ACCOUNT_NAME}@${DEPLOY_PROJECT}.iam.gserviceaccount.com"
WORKER_SERVICE_ACCOUNT="${WORKER_SERVICE_ACCOUNT_NAME}@${DEPLOY_PROJECT}.iam.gserviceaccount.com"
JWT_SECRET="google-ads-automation-jwt-secret"
OPENAI_SECRET="openai-bmw-runner-api-key"
REFRESH_SECRET="google-ads-automation-refresh-token"
OAUTH_CLIENT_ID_SECRET="google-ads-automation-oauth-client-id"
OAUTH_CLIENT_SECRET="google-ads-automation-oauth-client-secret"

required_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "$1 is required." >&2
    exit 1
  }
}

required_command gcloud
required_command git
required_command python3
required_command openssl

gcloud config set project "${DEPLOY_PROJECT}" >/dev/null
gcloud services enable \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com

SERVICE_CONFIG="$(mktemp)"
trap 'rm -f "${SERVICE_CONFIG}"' EXIT
gcloud run services describe "${INTERACTIVE_SERVICE}" \
  --region="${DEPLOY_REGION}" \
  --format=json >"${SERVICE_CONFIG}"

plain_env() {
  python3 - "${SERVICE_CONFIG}" "$1" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    service = json.load(handle)
name = sys.argv[2]
containers = service.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
for item in (containers[0].get("env", []) if containers else []):
    if item.get("name") == name:
        print(item.get("value", ""))
        break
PY
}

secret_env() {
  python3 - "${SERVICE_CONFIG}" "$1" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    service = json.load(handle)
name = sys.argv[2]
containers = service.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
for item in (containers[0].get("env", []) if containers else []):
    if item.get("name") == name:
        ref = item.get("valueFrom", {}).get("secretKeyRef", {})
        print(ref.get("name") or ref.get("secret") or "")
        break
PY
}

PORTAL_URL="$(plain_env RECOMMENDATION_CENTER_URL)"
DEVELOPER_TOKEN_SECRET="$(secret_env GOOGLE_ADS_DEVELOPER_TOKEN)"
PORTAL_INGESTION_SECRET="$(secret_env RECOMMENDATION_CENTER_INGESTION_KEY)"
PORTAL_BYPASS_SECRET="$(secret_env RECOMMENDATION_CENTER_SIWC_BYPASS_TOKEN)"

PORTAL_URL="${PORTAL_URL:-https://google-ads-recommendations.davidnjohnson.chatgpt.site}"

for required_value in \
  DEVELOPER_TOKEN_SECRET \
  PORTAL_INGESTION_SECRET \
  PORTAL_BYPASS_SECRET; do
  if [[ -z "${!required_value}" ]]; then
    echo "Could not derive ${required_value} from ${INTERACTIVE_SERVICE}." >&2
    exit 1
  fi
done

ensure_prompted_secret() {
  local secret_name="$1"
  local prompt="$2"
  if gcloud secrets describe "${secret_name}" >/dev/null 2>&1; then
    return
  fi
  local secret_value
  read -rsp "${prompt}: " secret_value
  echo
  if [[ -z "${secret_value}" ]]; then
    echo "${secret_name} cannot be empty." >&2
    exit 1
  fi
  printf '%s' "${secret_value}" | gcloud secrets create "${secret_name}" \
    --replication-policy=automatic \
    --data-file=-
  unset secret_value
}

if ! gcloud secrets describe "${JWT_SECRET}" >/dev/null 2>&1; then
  JWT_VALUE="$(openssl rand -base64 48 | tr -d '\n')"
  printf '%s' "${JWT_VALUE}" | gcloud secrets create "${JWT_SECRET}" \
    --replication-policy=automatic \
    --data-file=-
  unset JWT_VALUE
fi

ensure_prompted_secret "${OPENAI_SECRET}" "OpenAI project API key"
ensure_prompted_secret "${OAUTH_CLIENT_ID_SECRET}" "Dedicated Google Ads automation OAuth client ID"
ensure_prompted_secret "${OAUTH_CLIENT_SECRET}" "Dedicated Google Ads automation OAuth client secret"
ensure_prompted_secret "${REFRESH_SECRET}" "Google Ads OAuth refresh token"

ensure_service_account() {
  local account_name="$1"
  local display_name="$2"
  if ! gcloud iam service-accounts describe \
    "${account_name}@${DEPLOY_PROJECT}.iam.gserviceaccount.com" >/dev/null 2>&1; then
    gcloud iam service-accounts create "${account_name}" \
      --display-name="${display_name}"
  fi
}

ensure_service_account "${MCP_SERVICE_ACCOUNT_NAME}" "Google Ads automation MCP"
ensure_service_account "${WORKER_SERVICE_ACCOUNT_NAME}" "BMW Ads recommendation worker"

grant_secret_access() {
  local secret_name="$1"
  local service_account="$2"
  gcloud secrets add-iam-policy-binding "${secret_name}" \
    --member="serviceAccount:${service_account}" \
    --role=roles/secretmanager.secretAccessor >/dev/null
}

for secret_name in \
  "${JWT_SECRET}" \
  "${OAUTH_CLIENT_ID_SECRET}" \
  "${OAUTH_CLIENT_SECRET}" \
  "${REFRESH_SECRET}" \
  "${DEVELOPER_TOKEN_SECRET}" \
  "${PORTAL_INGESTION_SECRET}" \
  "${PORTAL_BYPASS_SECRET}"; do
  grant_secret_access "${secret_name}" "${MCP_SERVICE_ACCOUNT}"
done
grant_secret_access "${JWT_SECRET}" "${WORKER_SERVICE_ACCOUNT}"
grant_secret_access "${OPENAI_SECRET}" "${WORKER_SERVICE_ACCOUNT}"

if ! gcloud artifacts repositories describe "${ARTIFACT_REPOSITORY}" \
  --location="${DEPLOY_REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${ARTIFACT_REPOSITORY}" \
    --repository-format=docker \
    --location="${DEPLOY_REGION}" \
    --description="Constellation recommendation automation images"
fi

REVISION="$(git rev-parse --short=12 HEAD)"
IMAGE_ROOT="${DEPLOY_REGION}-docker.pkg.dev/${DEPLOY_PROJECT}/${ARTIFACT_REPOSITORY}"
MCP_IMAGE="${IMAGE_ROOT}/google-ads-mcp-automation:${REVISION}"
WORKER_IMAGE="${IMAGE_ROOT}/bmw-ads-recommendation-worker:${REVISION}"

gcloud builds submit --tag "${MCP_IMAGE}" .
gcloud builds submit \
  --config=cloudbuild.worker.yaml \
  --substitutions="_IMAGE=${WORKER_IMAGE}" \
  .

gcloud run deploy "${AUTOMATION_SERVICE}" \
  --image="${MCP_IMAGE}" \
  --region="${DEPLOY_REGION}" \
  --service-account="${MCP_SERVICE_ACCOUNT}" \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=2 \
  --concurrency=10 \
  --timeout=300 \
  --memory=512Mi \
  --cpu=1 \
  --set-env-vars="GOOGLE_PROJECT_ID=${DEPLOY_PROJECT},GOOGLE_ADS_MCP_AUTH_MODE=service_jwt,GOOGLE_ADS_MCP_TOOLS_CONFIG=ads_mcp/worker_tools_config.yaml,GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS=${BMW_CUSTOMER_ID},RECOMMENDATION_CENTER_ALLOWED_CUSTOMER_IDS=${BMW_CUSTOMER_ID},GOOGLE_ADS_LOGIN_CUSTOMER_ID=4599605095,RECOMMENDATION_CENTER_URL=${PORTAL_URL}" \
  --set-secrets="GOOGLE_ADS_DEVELOPER_TOKEN=${DEVELOPER_TOKEN_SECRET}:latest,GOOGLE_ADS_SERVICE_OAUTH_CLIENT_ID=${OAUTH_CLIENT_ID_SECRET}:latest,GOOGLE_ADS_SERVICE_OAUTH_CLIENT_SECRET=${OAUTH_CLIENT_SECRET}:latest,GOOGLE_ADS_SERVICE_REFRESH_TOKEN=${REFRESH_SECRET}:latest,GOOGLE_ADS_MCP_SERVICE_JWT_SECRET=${JWT_SECRET}:latest,RECOMMENDATION_CENTER_INGESTION_KEY=${PORTAL_INGESTION_SECRET}:latest,RECOMMENDATION_CENTER_SIWC_BYPASS_TOKEN=${PORTAL_BYPASS_SECRET}:latest"

MCP_URL="$(gcloud run services describe "${AUTOMATION_SERVICE}" \
  --region="${DEPLOY_REGION}" \
  --format='value(status.url)')/mcp"

gcloud run jobs deploy "${WORKER_JOB}" \
  --image="${WORKER_IMAGE}" \
  --region="${DEPLOY_REGION}" \
  --service-account="${WORKER_SERVICE_ACCOUNT}" \
  --max-retries=0 \
  --task-timeout=1800s \
  --memory=1Gi \
  --cpu=1 \
  --set-env-vars="GOOGLE_ADS_MCP_SERVICE_URL=${MCP_URL},OPENAI_MODEL=gpt-5.6" \
  --set-secrets="OPENAI_API_KEY=${OPENAI_SECRET}:latest,GOOGLE_ADS_MCP_SERVICE_JWT_SECRET=${JWT_SECRET}:latest"

echo
echo "Deployment complete. No schedule or Google Ads change was created."
echo "Automation MCP: ${MCP_URL}"
echo "BMW worker job: ${WORKER_JOB}"
echo "Manual canary command:"
echo "gcloud run jobs execute ${WORKER_JOB} --region=${DEPLOY_REGION} --wait"
