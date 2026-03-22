"""
AWS Batch Job: iot-csv-processor

Processa arquivos CSV de sensores IoT diretamente do S3.
Executa como container Fargate — sem limite de 15 minutos do Lambda.

Variáveis de ambiente:
  JOB_ID         — ID único do job (passado pelo Step Functions)
  S3_BUCKET      — Bucket S3 com o arquivo de entrada
  S3_KEY         — Chave do arquivo CSV no S3
  DYNAMODB_TABLE — Tabela para salvar resultados (passada via job definition)
"""

import os
import csv
import time
import boto3
import logging
from io import StringIO
from decimal import Decimal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main():
    job_id    = os.environ["JOB_ID"]
    s3_bucket = os.environ["S3_BUCKET"]
    s3_key    = os.environ["S3_KEY"]
    table_name = os.environ["DYNAMODB_TABLE"]

    log.info(f"Iniciando job {job_id}")
    log.info(f"Arquivo: s3://{s3_bucket}/{s3_key}")

    s3_client  = boto3.client("s3")
    dynamodb   = boto3.resource("dynamodb")
    table      = dynamodb.Table(table_name)

    # ── Download do CSV ───────────────────────────────────────────
    log.info("Baixando arquivo do S3...")
    t_start = time.time()

    response = s3_client.get_object(Bucket=s3_bucket, Key=s3_key)
    raw_content = response["Body"].read()

    # Detecta encoding — tenta UTF-8, fallback para latin-1
    try:
        content = raw_content.decode("utf-8")
    except UnicodeDecodeError:
        content = raw_content.decode("latin-1")

    file_size_mb = len(raw_content) / (1024 * 1024)
    log.info(f"Arquivo baixado: {file_size_mb:.1f} MB")

    # ── Processamento linha a linha ───────────────────────────────
    log.info("Processando registros IoT...")

    reader     = csv.DictReader(StringIO(content))
    records    = 0
    anomalies  = 0
    sensors    = set()

    for row in reader:
        records += 1

        # Coleta sensores únicos
        sensor_id = row.get("sensor_id") or row.get("device_id") or row.get("id")
        if sensor_id:
            sensors.add(sensor_id)

        # Detecta anomalias por temperatura acima de threshold
        try:
            temp = float(row.get("temperature") or row.get("temp") or row.get("value") or 0)
            if temp > 85.0:
                anomalies += 1
        except (ValueError, TypeError):
            pass

        # Log de progresso a cada 500k registros
        if records % 500_000 == 0:
            elapsed = time.time() - t_start
            log.info(f"  {records:,} registros processados ({elapsed:.1f}s)...")

    t_processing = round(time.time() - t_start, 2)

    log.info(f"Concluído: {records:,} registros | {anomalies:,} anomalias | {len(sensors)} sensores únicos | {t_processing}s")

    # ── Salva resultados no DynamoDB ──────────────────────────────
    log.info(f"Salvando resultados no DynamoDB (job_id={job_id})...")

    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression=(
            "SET records_processed    = :r, "
            "    anomalies_detected   = :a, "
            "    unique_sensors       = :u, "
            "    processing_time_seconds = :t, "
            "    file_size_mb         = :f"
        ),
        ExpressionAttributeValues={
            ":r": records,
            ":a": anomalies,
            ":u": len(sensors),
            ":t": Decimal(str(t_processing)),
            ":f": Decimal(f"{file_size_mb:.2f}"),
        },
    )

    log.info("Resultados salvos com sucesso.")
    log.info(f"=== JOB {job_id} CONCLUÍDO ===")


if __name__ == "__main__":
    main()
