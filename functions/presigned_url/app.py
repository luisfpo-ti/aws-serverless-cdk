"""
Lambda: recon-presigned-url
POST /upload

Recebe: { "file_name": "extrato_janeiro.csv", "file_size": 5242880 }
Retorna: { "upload_url": "https://...", "job_id": "uuid", "s3_key": "input/uuid/extrato_janeiro.csv" }

Fluxo:
  1. Gera um UUID como job_id
  2. Cria registro no DynamoDB com status PENDING
  3. Gera presigned URL para PUT direto no S3 (evita passar pelo Lambda)

IMPORTANTE — endpoint regional:
  O cliente S3 usa o endpoint regional explícito (s3.<region>.amazonaws.com)
  para que a presigned URL gerada já aponte para a região correta.
  Sem isso, o boto3 usa o endpoint global (s3.amazonaws.com) que redireciona
  para a região do bucket — e esse redirect quebra o CORS no browser.
"""

import json
import os
import uuid
import boto3
from botocore.config import Config
from datetime import datetime, timezone

# Usa o endpoint regional explícito para evitar redirect CORS
# Ex: https://bucket.s3.us-east-2.amazonaws.com/... em vez de s3.amazonaws.com
REGION = os.environ.get("AWS_REGION", "us-east-1")

s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=Config(signature_version="s3v4"),
)
dynamodb = boto3.resource("dynamodb")

BUCKET_NAME = os.environ["BUCKET_NAME"]
TABLE_NAME  = os.environ["TABLE_NAME"]
PRESIGNED_URL_EXPIRY = 300  # 5 minutos


def lambda_handler(event, context):
    try:
        body      = json.loads(event.get("body") or "{}")
        file_name = body.get("file_name", "upload.csv").strip()
        file_size = int(body.get("file_size", 0))

        if not file_name:
            return _response(400, {"error": "file_name é obrigatório"})

        job_id      = str(uuid.uuid4())
        s3_key      = f"input/{job_id}/{file_name}"
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

        # Gera presigned URL para PUT direto no S3 (sem trafegar pelo Lambda/API)
        # O endpoint regional garante que a URL não cause redirect e quebre o CORS
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": BUCKET_NAME,
                "Key":    s3_key,
            },
            ExpiresIn=PRESIGNED_URL_EXPIRY,
        )

        print(f"[INFO] Presigned URL gerada para job {job_id} | região: {REGION}")

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
            "Content-Type":                "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(body),
    }
