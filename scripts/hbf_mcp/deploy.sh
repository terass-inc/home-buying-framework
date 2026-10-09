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
  --memory 512Mi --cpu 1 --min-instances 0 --max-instances 2 --concurrency 40 --timeout 30 \
  --set-env-vars "^@^HBF_MCP_PATH=/mcp/home-buying@HBF_RATE_PER_MIN=60@HBF_TRUSTED_PROXY_HOPS=2@HBF_ALLOWED_HOSTS=${HOSTS}"

# 許可するのは terass.house 系のホストだけにし、Cloud Run の *.run.app への直接アクセスは受け付けない（421）。
# すべての利用が Firebase Hosting 経由になるので、回数制限の接続元（X-Forwarded-For の末尾から2番目）を一意に決められる。
# 初回はステージングで、HBF_DEBUG_FORWARDING=1 を付けて Firebase 経由の Host と X-Forwarded-For の件数を確かめる:
#   gcloud run services update "$SERVICE" --update-env-vars HBF_DEBUG_FORWARDING=1 ...
#   gcloud logging read 'jsonPayload.event="hbf_forwarding_debug"' --project "$PROJECT" --limit 5
# Host が terass.house 系で、X-Forwarded-For が2件（利用者, Firebase）なら、この設定のまま本番に出す。
# 違っていたら HBF_ALLOWED_HOSTS と HBF_TRUSTED_PROXY_HOPS を実際の形に合わせ、確認後に HBF_DEBUG_FORWARDING を外す。
echo "デプロイ完了: https://terass.house/mcp/home-buying（Firebase Hosting の転送経由）"
