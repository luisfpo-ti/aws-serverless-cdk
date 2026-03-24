"""
Lambda: recon-save-results
Chamado pelo Step Functions após conclusão do AWS Batch job.

Recebe:
  { "job_id": "...", "file_name": "...", "status": "PROCESSED" | "FAILED", "error": {...} }

Atualiza o registro no DynamoDB com status final e timestamp de conclusão.
Os resultados detalhados (total_registros, saldo, divergencias etc.) foram gravados
diretamente pelo Batch job — este Lambda apenas finaliza o status.
"""

import json
import os
import boto3
from datetime import datetime, timezone
from decimal import Decimal

dynamodb = boto3.resource("dynamodb")
TABLE_NAME = os.environ["TABLE_NAME"]


def lambda_handler(event, context):
    # Step Functions envolve o payload em "Payload" quando usa arn:aws:states:::lambda:invoke
    payload = event.get("Payload", event)

    job_id    = payload.get("job_id")
    status    = payload.get("status", "PROCESSED")
    error_msg = str(payload.get("error", ""))[:500] if payload.get("error") else None

    if not job_id:
        raise ValueError("job_id não encontrado no payload")

    print(f"[INFO] Atualizando job {job_id} → status={status}")

    table = dynamodb.Table(TABLE_NAME)

    update_expr = "SET #st = :s, processed_at = :t"
    expr_values = {
        ":s": status,
        ":t": datetime.now(timezone.utc).isoformat(),
    }
    expr_names = {"#st": "status"}

    if error_msg:
        update_expr += ", error_message = :e"
        expr_values[":e"] = error_msg

    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )

    print(f"[INFO] Job {job_id} atualizado para {status}")

    return {
        "job_id": job_id,
        "status": status,
        "processed_at": expr_values[":t"],
    }
