"""
Lambda: iot-trigger-pipeline
Trigger: S3 ObjectCreated em input/*

Quando um arquivo CSV chega no S3:
  1. Extrai job_id e file_name do S3 key (formato: input/{job_id}/{file_name})
  2. Atualiza status do job para PROCESSING no DynamoDB
  3. Inicia execução da Step Function com os dados do arquivo
"""

import json
import os
import urllib.parse
import boto3
from datetime import datetime, timezone

dynamodb = boto3.resource("dynamodb")
sfn = boto3.client("stepfunctions")

TABLE_NAME         = os.environ["TABLE_NAME"]
STATE_MACHINE_ARN  = os.environ["STATE_MACHINE_ARN"]


def lambda_handler(event, context):
    for record in event.get("Records", []):
        try:
            # Decodifica o S3 key (pode conter caracteres URL-encoded)
            raw_key = record["s3"]["object"]["key"]
            s3_key  = urllib.parse.unquote_plus(raw_key)
            bucket  = record["s3"]["bucket"]["name"]

            # Formato esperado: input/{job_id}/{file_name}
            parts    = s3_key.split("/")
            job_id   = parts[1]
            file_name = "/".join(parts[2:])

            print(f"[INFO] Arquivo recebido: s3://{bucket}/{s3_key} | job_id={job_id}")

            # Atualiza status para PROCESSING no DynamoDB
            table = dynamodb.Table(TABLE_NAME)
            table.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET #st = :s, processing_started_at = :t",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":s": "PROCESSING",
                    ":t": datetime.now(timezone.utc).isoformat(),
                },
            )

            # Inicia Step Functions passando os dados necessários para o Batch job
            execution_name = f"job-{job_id[:8]}"
            sfn.start_execution(
                stateMachineArn=STATE_MACHINE_ARN,
                name=execution_name,
                input=json.dumps({
                    "job_id":    job_id,
                    "file_name": file_name,
                    "bucket":    bucket,
                    "s3_key":    s3_key,
                }),
            )

            print(f"[INFO] Step Functions iniciado: {execution_name}")

        except Exception as e:
            print(f"[ERROR] Falha ao processar record: {e}")
            raise

    return {"statusCode": 200}
