"""
AWS Batch Job: bank-reconciliation-processor

Processa extratos bancários CSV diretamente do S3.
Executa como container Fargate — sem limite de 15 minutos do Lambda.

Colunas esperadas no CSV:
  data        - data da transação (YYYY-MM-DD)
  descricao   - descrição do lançamento
  valor       - valor em reais (positivo = crédito, negativo = débito)
  tipo        - C (Crédito) ou D (Débito)
  referencia  - código de referência / número do documento

Variáveis de ambiente:
  JOB_ID         — ID único do job (passado pelo Step Functions)
  S3_BUCKET      — Bucket S3 com o arquivo de entrada
  S3_KEY         — Chave do arquivo CSV no S3
  DYNAMODB_TABLE — Tabela para salvar resultados
"""

import os
import csv
import time
import boto3
import logging
from io import StringIO
from decimal import Decimal
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def conciliar(rows: list) -> dict:
    """
    Regras de conciliação:
    - Duplicatas: mesma referência aparece mais de uma vez
    - Divergências: tipo C com valor negativo, ou tipo D com valor positivo
    """
    total_credito  = 0.0
    total_debito   = 0.0
    divergencias   = 0
    refs           = defaultdict(int)

    for row in rows:
        try:
            valor = float(row.get("valor", 0))
            tipo  = row.get("tipo", "").strip().upper()
            ref   = row.get("referencia", "").strip()

            if tipo == "C":
                total_credito += abs(valor)
                if valor < 0:
                    divergencias += 1
            elif tipo == "D":
                total_debito += abs(valor)
                if valor > 0:
                    divergencias += 1
            else:
                divergencias += 1

            if ref:
                refs[ref] += 1
        except (ValueError, TypeError):
            divergencias += 1

    duplicatas  = sum(1 for v in refs.values() if v > 1)
    saldo       = total_credito - total_debito
    registros_ok = max(len(rows) - duplicatas - divergencias, 0)

    return {
        "total_registros": len(rows),
        "registros_ok":    registros_ok,
        "total_credito":   round(total_credito, 2),
        "total_debito":    round(total_debito, 2),
        "saldo":           round(saldo, 2),
        "duplicatas":      duplicatas,
        "divergencias":    divergencias,
    }


def main():
    job_id     = os.environ["JOB_ID"]
    s3_bucket  = os.environ["S3_BUCKET"]
    s3_key     = os.environ["S3_KEY"]
    table_name = os.environ["DYNAMODB_TABLE"]

    log.info(f"Iniciando conciliação | job_id={job_id}")
    log.info(f"Extrato: s3://{s3_bucket}/{s3_key}")

    s3_client = boto3.client("s3")
    dynamodb  = boto3.resource("dynamodb")
    table     = dynamodb.Table(table_name)

    # ── Download do extrato CSV ───────────────────────────────────
    log.info("Baixando extrato do S3...")
    t_start = time.time()

    response    = s3_client.get_object(Bucket=s3_bucket, Key=s3_key)
    raw_content = response["Body"].read()

    try:
        content = raw_content.decode("utf-8")
    except UnicodeDecodeError:
        content = raw_content.decode("latin-1")

    file_size_mb = len(raw_content) / (1024 * 1024)
    log.info(f"Arquivo baixado: {file_size_mb:.1f} MB")

    # ── Leitura e conciliação ─────────────────────────────────────
    log.info("Processando registros...")
    reader = csv.DictReader(StringIO(content))
    rows   = list(reader)

    # Log de progresso para arquivos grandes
    total = len(rows)
    log.info(f"{total:,} registros carregados")
    if total > 500_000:
        log.info("Arquivo grande detectado — processamento em lote...")

    resultado = conciliar(rows)
    t_total   = round(time.time() - t_start, 2)

    log.info(
        f"Conciliação concluída: "
        f"{resultado['total_registros']:,} registros | "
        f"OK: {resultado['registros_ok']:,} | "
        f"Duplicatas: {resultado['duplicatas']} | "
        f"Divergências: {resultado['divergencias']} | "
        f"Saldo: R$ {resultado['saldo']:,.2f} | "
        f"{t_total}s"
    )

    # ── Salva resultados no DynamoDB ──────────────────────────────
    log.info(f"Salvando resultados no DynamoDB (job_id={job_id})...")

    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression=(
            "SET total_registros       = :total, "
            "    registros_ok          = :ok, "
            "    total_credito         = :credito, "
            "    total_debito          = :debito, "
            "    saldo                 = :saldo, "
            "    duplicatas            = :dup, "
            "    divergencias          = :div, "
            "    processing_time_s     = :t, "
            "    file_size_mb          = :f"
        ),
        ExpressionAttributeValues={
            ":total":   resultado["total_registros"],
            ":ok":      resultado["registros_ok"],
            ":credito": Decimal(str(resultado["total_credito"])),
            ":debito":  Decimal(str(resultado["total_debito"])),
            ":saldo":   Decimal(str(resultado["saldo"])),
            ":dup":     resultado["duplicatas"],
            ":div":     resultado["divergencias"],
            ":t":       Decimal(str(t_total)),
            ":f":       Decimal(f"{file_size_mb:.2f}"),
        },
    )

    log.info("Resultados salvos com sucesso.")
    log.info(f"=== JOB {job_id} CONCLUÍDO ===")


if __name__ == "__main__":
    main()
