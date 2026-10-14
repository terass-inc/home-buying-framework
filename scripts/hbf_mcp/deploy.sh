#!/usr/bin/env bash
# MCP サーバーを terass-house プロジェクトの Cloud Run にデプロイする。
# terass.house の firebase.json が /mcp/home-buying をこのサービスに転送する（terass-inc/terass-house PR #139）。
# 必要な権限: Cloud Run 管理者・サービス アカウント ユーザー・Cloud Build 編集者・Artifact Registry 管理者・
# 実行用のサービスアカウント（権限なし）は情シスが作成済み。吉岡にはこのアカウントに限ってサービス アカウント ユーザーが付いている
set -euo pipefail
PROJECT="${PROJECT:-terass-house}"
REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-home-buying-framework-mcp}"
SA="${SA:-home-buying-mcp@${PROJECT}.iam.gserviceaccount.com}"
DEBUG_FORWARDING="${DEBUG_FORWARDING:-0}"   # 初回は 1 にして、Firebase 経由の転送の形を確かめる
HOSTS="terass.house,terass-house-staging.web.app,terass-house.web.app"
cd "$(dirname "$0")/../.."

# API の有効化は情シスが実施済み

# 実行用のサービスアカウントは権限を何も持たない（既定のサービスアカウントはプロジェクトの編集権限を持つため使わない）。
# サーバーは外部のサービスを呼ばず、ログは標準出力に書くだけなので、権限は不要。

# 1) ビルド。`gcloud run deploy --source` は既定バケットの持ち主を確かめられないと別名のバケットを作ろうとして
#    権限エラーになる（2026-10-14）。そのため、情シスが用意したバケットを明示してビルドし、イメージを指定してデプロイする。
#    ログは Cloud Logging だけに出す（ログ用バケットの作成を避ける）。
BUCKET="${BUCKET:-run-sources-${PROJECT}-${REGION}}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/${SERVICE}:$(git rev-parse --short HEAD)$(git diff --quiet || echo -dirty)"
CB=$(mktemp -t cloudbuild.XXXX.yaml)
cat > "$CB" <<YAML
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-t", "${IMAGE}", "."]
images: ["${IMAGE}"]
options:
  logging: CLOUD_LOGGING_ONLY
YAML
gcloud builds submit . --project "$PROJECT" --region "$REGION" --config "$CB" \
  --gcs-source-staging-dir "gs://${BUCKET}/source"
rm -f "$CB"

# 2) デプロイ
gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --image "$IMAGE" \
  --service-account "$SA" --allow-unauthenticated --ingress all \
  --memory 512Mi --cpu 1 --min-instances 0 --max-instances 2 --concurrency 40 --timeout 30 \
  --set-env-vars "^@^HBF_MCP_PATH=/mcp/home-buying@HBF_RATE_PER_MIN=60@HBF_TRUSTED_PROXY_HOPS=2@HBF_DEBUG_FORWARDING=${DEBUG_FORWARDING}@HBF_ALLOWED_HOSTS=${HOSTS}"

# 許可するのは terass.house 系のホストだけにし、Cloud Run の *.run.app への直接アクセスは受け付けない（421）。
# すべての利用が Firebase Hosting 経由になるので、回数制限の接続元（X-Forwarded-For の末尾から2番目）を一意に決められる。
# 初回はステージングで、HBF_DEBUG_FORWARDING=1 を付けて Firebase 経由の Host と X-Forwarded-For の件数を確かめる:
#   gcloud run services update "$SERVICE" --update-env-vars HBF_DEBUG_FORWARDING=1 ...
#   gcloud logging read 'jsonPayload.event="hbf_forwarding_debug"' --project "$PROJECT" --limit 5
# Host が terass.house 系で、X-Forwarded-For が2件（利用者, Firebase）なら、この設定のまま本番に出す。
# 違っていたら HBF_ALLOWED_HOSTS と HBF_TRUSTED_PROXY_HOPS を実際の形に合わせ、確認後に HBF_DEBUG_FORWARDING を外す。
echo "デプロイ完了: https://terass.house/mcp/home-buying（Firebase Hosting の転送経由）"
