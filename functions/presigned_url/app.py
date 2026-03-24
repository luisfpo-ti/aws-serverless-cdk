"""
Lambda: recon-presigned-url
POST /upload

Recebe: { "file_name": "extrato_janeiro.csv", "file_size": 5242880 }
Retorna: { "upload_url": "https://...", "job_id": "uuid", "s3_key": "input/uuid/extrato_janeiro.csv" }

Fluxo:
  1. Gera um UUID como job_id
  2. Cria registro no DynamoDB com status PENDING
  3. Gera presigned URL para PUT direto no S3 (evita passar pelo Lambda)
"""

import json
import os
import uuid
import boto3
from datetime import datetime, timezone

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")

BUCKET_NAME = os.environ["BUCKET_NAME"]
TABLE_NAME = os.environ["TABLE_NAME"]
PRESIGNED_URL_EXPIRY = 300  # 5 minutos


def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        file_name = body.get("file_name", "upload.csv").strip()
        file_size = int(body.get("file_size", 0))

        if not file_name:
            return _response(400, {"error": "file_name é obrigatório"})

        # Gera identificadores únicos
        job_id = str(uuid.uuid4())
        s3_key = f"input/{job_id}/{file_name}"
        uploaded_at = datetime.now(timezone.utc).isoformat()

        # Cria registro no DynamoDB com status PENDING
        table = dynamodb.Table(TABLE_NAME)
        table.put_item(Item={
            "job_id":      job_id,
            "file_name":   file_name,
            "file_size":   file_size,
            "s3_key":      s3_key,
            "status":      "PENDING",
            "uploaded_at": uploaded_at,
        })

        # Gera presigned URL para PUT direto no S3 (sem passar pelo Lambda)
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket":      BUCKET_NAME,
                "Key":         s3_key,
                "ContentType": "text/csv",
            },
            ExpiresIn=PRESIGNED_URL_EXPIRY,
        )

        return _response(200, {
            "job_id":     job_id,
            "upload_url": upload_url,
            "s3_key":     s3_key,
            "expires_in": PRESIGNED_URL_EXPIRY,
        })

    except Exception as e:
        print(f"[ERROR] {e}")
        return _response(500, {"error": "Erro interno ao gerar upload URL"})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(body),
    }
