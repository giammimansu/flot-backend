#!/usr/bin/env bash
# Post-deploy script to configure S3 → Lambda notification
# This is separate from CloudFormation to avoid circular dependencies
# Usage: ./scripts/setup-s3-notification.sh [stage]

set -euo pipefail

STAGE="${1:-dev}"
STACK_NAME="flot-backend-${STAGE}"

echo "🔍 Fetching stack outputs for ${STACK_NAME}..."

BUCKET_NAME=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --query "Stacks[0].Outputs[?OutputKey=='MediaBucketName'].OutputValue" \
  --output text)

LAMBDA_ARN=$(aws lambda get-function \
  --function-name "flot-process-photo-${STAGE}" \
  --query "Configuration.FunctionArn" \
  --output text)

echo "📦 Bucket: ${BUCKET_NAME}"
echo "⚡ Lambda: ${LAMBDA_ARN}"

echo "🔔 Configuring S3 notification..."

aws s3api put-bucket-notification-configuration \
  --bucket "${BUCKET_NAME}" \
  --notification-configuration "{
    \"LambdaFunctionConfigurations\": [
      {
        \"LambdaFunctionArn\": \"${LAMBDA_ARN}\",
        \"Events\": [\"s3:ObjectCreated:*\"],
        \"Filter\": {
          \"Key\": {
            \"FilterRules\": [
              {\"Name\": \"prefix\", \"Value\": \"photos/\"},
              {\"Name\": \"suffix\", \"Value\": \"original.webp\"}
            ]
          }
        }
      }
    ]
  }"

echo "✅ S3 notification configured! Photos uploaded to photos/*/original.webp will trigger ProcessPhoto Lambda."
