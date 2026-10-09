#!/usr/bin/env bash
# MCP サーバーを terass-house プロジェクトの Cloud Run にデプロイする。
# terass.house の firebase.json が /mcp/home-buying をこのサービスに転送する（terass-inc/terass-house PR #139）。
# 必要な権限: Cloud Run 管理者・サービス アカウント ユーザー・Cloud Build 編集者・Artifact Registry 管理者・
#            サービス アカウント管理者（初回のみ。実行用のサービスアカウントを作るため）
set -euo pipefail
PROJECT="${PROJECT:-terass-house}"
REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-home-buying-framework-mcp}"
SA_NAME="${SA_NAME:-hbf-mcp-runtime}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
HOSTS="terass.house,terass-house-staging.web.app,terass-house.web.app"
cd "$(dirname "$0")/../.."

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com --project "$PROJECT"

# 実行用のサービスアカウントは権限を何も持たせない（既定のサービスアカウントはプロジェクトの編集権限を持つため使わない）。
# サーバーは外部のサービスを呼ばず、ログは標準出力に書くだけなので、権限は不要。
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" --display-name "home-buying-framework MCP (no roles)"

gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --source . \
  --service-account "$SA" --allow-unauthenticated --ingress all \
  --memory 512Mi --cpu 1 --min-instances 0 --max-instances 5 --concurrency 40 --timeout 30 \
  --set-env-vars "^@^HBF_MCP_PATH=/mcp/home-buying@HBF_RATE_PER_MIN=60@HBF_ALLOWED_HOSTS=${HOSTS}"

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)')
# Firebase の転送経由でも直接でも受け付けるよう、Cloud Run 自身のホストも許可に加える
gcloud run services update "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --update-env-vars "^@^HBF_ALLOWED_HOSTS=${HOSTS},${URL#https://}"
echo "デプロイ完了: ${URL}/mcp/home-buying"
