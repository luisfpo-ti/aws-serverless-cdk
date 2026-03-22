"""
Lambda: iot-get-status
GET /status

Retorna a lista de todos os jobs de processamento, ordenados por data de upload (mais recente primeiro).
O portal usa este endpoint para polling a cada 3s enquanto houver jobs PENDING/PROCESSING.
"""

import json
import os
import boto3
from boto3.dynamodb.conditions import Attr
from decimal import Decimal

dynamodb = boto3.resource("dynamodb")
TABLE_NAME = os.environ["TABLE_NAME"]


class DecimalEncoder(json.JSONEncoder):
    """Converte Decimal (DynamoDB) para float ao serializar JSON."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def lambda_handler(event, context):
    try:
        table = dynamodb.Table(TABLE_NAME)

        # Scan simples — adequado para demo (poucos registros)
        response = table.scan()
        items = response.get("Items", [])

        # Pagina caso haja mais de 1MB de dados
        while "LastEvaluatedKey" in response:
            response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))

        # Ordena por data de upload (mais recente primeiro)
        items.sort(key=lambda x: x.get("uploaded_at", ""), reverse=True)

        return _response(200, {"jobs": items, "total": len(items)})

    except Exception as e:
        print(f"[ERROR] {e}")
        return _response(500, {"error": "Erro ao buscar status dos jobs"})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(body, cls=DecimalEncoder),
    }
