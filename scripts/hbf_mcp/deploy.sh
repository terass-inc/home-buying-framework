#!/usr/bin/env bash
# MCP サーバーを terass-house プロジェクトの Cloud Run にデプロイする。
# terass.house の firebase.json が /mcp/home-buying をこのサービスに転送する（terass-inc/terass-house PR #139）。
# 必要な権限: Cloud Run 管理者・サービス アカウント ユーザー・Cloud Build 編集者・Artifact Registry 管理者
set -euo pipefail
PROJECT="${PROJECT:-terass-house}"
REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-home-buying-framework-mcp}"
cd "$(dirname "$0")/../.."

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com --project "$PROJECT"
gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --source . \
  --allow-unauthenticated --memory 512Mi --cpu 1 --min-instances 0 --max-instances 5 --concurrency 40 --timeout 60 \
  --set-env-vars "^@^HBF_MCP_PATH=/mcp/home-buying@HBF_ALLOWED_HOSTS=terass.house,terass-house-staging.web.app,terass-house.web.app"

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)')
HOST="${URL#https://}"
# Firebase の転送経由でも直接でも受け付けるよう、Cloud Run 自身のホストも許可に加える
gcloud run services update "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --update-env-vars "^@^HBF_ALLOWED_HOSTS=terass.house,terass-house-staging.web.app,terass-house.web.app,${HOST}"
echo "デプロイ完了: ${URL}/mcp/home-buying"
