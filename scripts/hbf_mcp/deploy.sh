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
# サーバーが呼ぶ外部サービスは GA4 の受け口（google-analytics.com、API シークレットで送る）だけで、ログは標準出力に書くので、権限は不要。

# GA4 への送信（ga4.py）。手元の環境変数 GA4_MEASUREMENT_ID と GA4_API_SECRET があれば Cloud Run に渡す。
# シークレットはリポジトリに書かない。なければ GA4 には送らず、Cloud Logging だけに記録する。
GA4_ENV=""
if [[ -n "${GA4_MEASUREMENT_ID:-}" && -n "${GA4_API_SECRET:-}" ]]; then
  GA4_ENV="@GA4_MEASUREMENT_ID=${GA4_MEASUREMENT_ID}@GA4_API_SECRET=${GA4_API_SECRET}"
else
  echo "GA4_MEASUREMENT_ID と GA4_API_SECRET がないので、GA4 には送らない設定でデプロイします"
fi

# 1) ビルド（2026-10-14 の初回デプロイで踏んだ権限の罠を避ける形）
#    - `gcloud run deploy --source` は既定バケットの持ち主を確かめられないと別名のバケットを作ろうとして失敗する
#    - `gcloud builds submit` は serviceusage.services.use を要求する
#    そのため、コミット済みのソースを固めて情シスが用意したバケットに置き、Cloud Build の API に
#    ビルド専用アカウント（権限は最小）を指定して直接依頼する。ログは Cloud Logging だけに出す。
BUCKET="${BUCKET:-run-sources-${PROJECT}-${REGION}}"
BUILD_SA="${BUILD_SA:-home-buying-mcp-build@${PROJECT}.iam.gserviceaccount.com}"
git diff --quiet && git diff --cached --quiet || { echo "未コミットの変更があります。コミットしてから実行してください"; exit 1; }
SHA=$(git rev-parse --short HEAD)
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/${SERVICE}:${SHA}"
TGZ=$(mktemp -t hbf-src.XXXX).tgz
git archive --format=tar.gz -o "$TGZ" HEAD
gcloud storage cp "$TGZ" "gs://${BUCKET}/source/${SHA}.tgz" --quiet
rm -f "$TGZ"
TOKEN=$(gcloud auth print-access-token)
BUILD_ID=$(python3 - "$TOKEN" "$PROJECT" "$REGION" "$BUCKET" "$SHA" "$IMAGE" "$BUILD_SA" <<'PY'
import json, sys, urllib.request, urllib.error
token, project, region, bucket, sha, image, sa = sys.argv[1:]
body = {"source": {"storageSource": {"bucket": bucket, "object": f"source/{sha}.tgz"}},
        "steps": [{"name": "gcr.io/cloud-builders/docker", "args": ["build", "-t", image, "."]}],
        "images": [image], "serviceAccount": f"projects/{project}/serviceAccounts/{sa}",
        "options": {"logging": "CLOUD_LOGGING_ONLY"}}
req = urllib.request.Request(f"https://cloudbuild.googleapis.com/v1/projects/{project}/locations/{region}/builds",
                             data=json.dumps(body).encode(), method="POST",
                             headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
try:
    print(json.load(urllib.request.urlopen(req))["metadata"]["build"]["id"])
except urllib.error.HTTPError as e:
    sys.exit(f"ビルドの依頼に失敗: {e.code} {e.read().decode()[:600]}")
PY
)
echo "ビルド中: ${BUILD_ID}"
while :; do
  STATUS=$(gcloud builds describe "$BUILD_ID" --project "$PROJECT" --region "$REGION" --format 'value(status)')
  case "$STATUS" in SUCCESS) break ;; FAILURE|INTERNAL_ERROR|TIMEOUT|CANCELLED|EXPIRED)
    echo "ビルド失敗: ${STATUS}（ログ: gcloud logging read 'resource.type=\"build\" AND resource.labels.build_id=\"${BUILD_ID}\"' --project ${PROJECT}）"; exit 1 ;; esac
  sleep 10
done
echo "ビルド完了: ${IMAGE}"

# 2) デプロイ
gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --image "$IMAGE" \
  --service-account "$SA" --allow-unauthenticated --ingress all \
  --memory 512Mi --cpu 1 --min-instances 0 --max-instances 2 --concurrency 40 --timeout 30 \
  --set-env-vars "^@^HBF_MCP_PATH=/mcp/home-buying@HBF_RATE_PER_MIN=60@HBF_TRUSTED_PROXY_HOPS=2@HBF_TRUST_FORWARDED_HOST=1@HBF_PROXY_RATE_PER_MIN=600@HBF_DEBUG_FORWARDING=${DEBUG_FORWARDING}@HBF_ALLOWED_HOSTS=${HOSTS}${GA4_ENV}"

# 許可するのは terass.house 系のホストだけにし、Cloud Run の *.run.app への直接アクセスは受け付けない（421）。
# すべての利用が Firebase Hosting 経由になるので、回数制限の接続元（X-Forwarded-For の末尾から2番目）を一意に決められる。
# 初回はステージングで、HBF_DEBUG_FORWARDING=1 を付けて Firebase 経由の Host と X-Forwarded-For の件数を確かめる:
#   gcloud run services update "$SERVICE" --update-env-vars HBF_DEBUG_FORWARDING=1 ...
#   gcloud logging read 'jsonPayload.event="hbf_forwarding_debug"' --project "$PROJECT" --limit 5
# 2026-10-14 の確認結果: Host は *.run.app、X-Forwarded-Host に元のドメイン、X-Forwarded-For は2件（利用者, Firebase）。
# そのため HBF_TRUST_FORWARDED_HOST=1 で X-Forwarded-Host を元のドメインとして扱い、末尾から2番目を利用者とする。
# 違っていたら HBF_ALLOWED_HOSTS と HBF_TRUSTED_PROXY_HOPS を実際の形に合わせ、確認後に HBF_DEBUG_FORWARDING を外す。
echo "デプロイ完了: https://terass.house/mcp/home-buying（Firebase Hosting の転送経由）"
