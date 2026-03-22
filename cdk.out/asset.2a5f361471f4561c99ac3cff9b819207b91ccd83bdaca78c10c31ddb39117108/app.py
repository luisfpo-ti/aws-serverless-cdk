"""
Lambda: iot-notify
Chamado pelo Step Functions como último passo do pipeline.

Lê o registro final do DynamoDB e publica uma notificação no SNS
com o resumo do processamento (registros processados, anomalias, tempo).
"""

import json
import os
import boto3
from decimal import Decimal

sns_client = boto3.client("sns")
dynamodb   = boto3.resource("dynamodb")

TOPIC_ARN  = os.environ["TOPIC_ARN"]
TABLE_NAME = os.environ["TABLE_NAME"]


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def lambda_handler(event, context):
    payload   = event.get("Payload", event)
    job_id    = payload.get("job_id")
    file_name = payload.get("file_name", "arquivo")

    if not job_id:
        raise ValueError("job_id não encontrado no payload")

    # Lê resultado final do DynamoDB
    table    = dynamodb.Table(TABLE_NAME)
    response = table.get_item(Key={"job_id": job_id})
    item     = response.get("Item", {})

    records    = item.get("records_processed", 0)
    anomalies  = item.get("anomalies_detected", 0)
    proc_time  = item.get("processing_time_seconds", 0)
    status     = item.get("status", "PROCESSED")

    message = (
        f"✅ Processamento concluído!\n\n"
        f"Arquivo:     {file_name}\n"
        f"Status:      {status}\n"
        f"Registros:   {int(records):,}\n"
        f"Anomalias:   {int(anomalies):,}\n"
        f"Tempo Batch: {float(proc_time):.1f}s\n\n"
        f"Job ID: {job_id}"
    )

    sns_client.publish(
        TopicArn=TOPIC_ARN,
        Subject=f"IoT Pipeline — {file_name} processado",
        Message=message,
    )

    print(f"[INFO] Notificação enviada para job {job_id}: {records} registros em {proc_time}s")

    return {"job_id": job_id, "notified": True}
