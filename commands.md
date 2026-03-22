# IoT Pipeline — Guia de Deploy e Demo

## Pré-requisitos

```bash
# AWS CLI configurado
aws configure

# Node.js (para CDK CLI)
node --version  # >= 18

# CDK CLI
npm install -g aws-cdk

# Docker (necessário para build da imagem do Batch)
docker info

# Python 3.10+
python3 --version
```

---

## 1. Setup do projeto

```bash
cd iot-pipeline

# Cria virtualenv e instala dependências CDK
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Bootstrap do CDK na conta (apenas 1x por conta/região)
cdk bootstrap
```

---

## 2. Deploy

```bash
# Visualiza o que será criado (opcional)
cdk diff

# Deploy completo — CDK faz build da imagem Docker e envia para ECR automaticamente
cdk deploy --outputs-file frontend/config.json
```

O `--outputs-file` gera o `frontend/config.json` com a URL da API para o portal usar automaticamente.

**Recursos criados:**
- S3 Bucket (input de CSVs)
- DynamoDB table `FileProcessingJobs`
- API Gateway REST API (2 endpoints)
- 5 Lambda functions
- AWS Batch (Fargate Compute Env + Job Queue + Job Definition)
- Step Functions state machine `iot-pipeline`
- SNS Topic para notificações
- VPC com subnets públicas (para o Batch)
- IAM Roles com least privilege

---

## 3. Rodar o Portal

```bash
cd frontend
python3 -m http.server 8080
# Acesse: http://localhost:8080
```

O portal carrega a API URL automaticamente do `config.json` gerado pelo `cdk deploy`.

---

## 4. Demo: Gerar CSV de teste

```bash
# Gera CSV de 100k registros IoT (~12 MB)
python3 - <<'EOF'
import csv, random, datetime, os

rows = 100_000
filename = f"sensors_{rows//1000}k.csv"

with open(filename, 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(['timestamp','sensor_id','temperature','humidity','pressure'])
    for i in range(rows):
        ts = datetime.datetime.utcnow().isoformat()
        writer.writerow([
            ts,
            f"sensor_{random.randint(1,500):04d}",
            round(random.gauss(65, 15), 2),   # ~2% acima de 85°C = anomalia
            round(random.uniform(30, 90), 2),
            round(random.uniform(980, 1020), 2),
        ])

print(f"Gerado: {filename} ({os.path.getsize(filename)/1024/1024:.1f} MB)")
EOF
```

Para simular arquivo grande (~500 MB):
```bash
# Gera 5M de registros
python3 -c "
import csv, random, sys
w = csv.writer(sys.stdout)
w.writerow(['timestamp','sensor_id','temperature','humidity'])
for i in range(5_000_000):
    w.writerow(['2025-01-01T00:00:00', f'sensor_{i%1000:04d}', round(random.gauss(65,15),2), round(random.uniform(30,90),2)])
" > sensors_5M.csv
```

---

## 5. Demo: Upload via curl (sem portal)

```bash
# Lê o API URL do config.json
API_URL=$(python3 -c "import json; d=json.load(open('frontend/config.json')); print(list(d.values())[0]['ApiUrl'].rstrip('/'))")

# 1. Solicita presigned URL
RESPONSE=$(curl -s -X POST "$API_URL/upload" \
  -H "Content-Type: application/json" \
  -d '{"file_name": "sensors_100k.csv", "file_size": 12000000}')

echo $RESPONSE | python3 -m json.tool
JOB_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
UPLOAD_URL=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['upload_url'])")

# 2. Upload direto no S3 via presigned URL (sem passar pelo Lambda/API)
curl -X PUT "$UPLOAD_URL" \
  -H "Content-Type: text/csv" \
  --upload-file sensors_100k.csv \
  --progress-bar

# 3. Verifica status
curl -s "$API_URL/status" | python3 -m json.tool
```

---

## 6. Monitorar execução

```bash
# Verifica status do job via API
curl -s "$API_URL/status" | python3 -c "
import sys, json
jobs = json.load(sys.stdin)['jobs']
for j in jobs:
    print(f\"{j['job_id'][:8]} | {j['file_name']:<30} | {j['status']:<12} | records: {j.get('records_processed','—')}\")"

# Resultado no DynamoDB diretamente
aws dynamodb get-item \
  --table-name FileProcessingJobs \
  --key "{\"job_id\": {\"S\": \"$JOB_ID\"}}" \
  --output json | python3 -m json.tool

# Step Functions — lista execuções
STATE_MACHINE_ARN=$(python3 -c "import json; d=json.load(open('frontend/config.json')); print(list(d.values())[0]['StateMachineArn'])")
aws stepfunctions list-executions \
  --state-machine-arn $STATE_MACHINE_ARN \
  --query "executions[:5].{name:name,status:status,start:startDate}" \
  --output table

# Logs do Batch job no CloudWatch
aws logs tail /aws/batch/job --follow --format short
```

---

## 7. Cleanup

```bash
# Remove toda a infraestrutura
cdk destroy

# O S3 bucket e DynamoDB são destruídos automaticamente (RemovalPolicy.DESTROY)
```

---

## Arquitetura resumida

```
Portal (HTML)
  │
  ├── POST /upload ──► API Gateway ──► Lambda presigned_url ──► DynamoDB (PENDING)
  │                                                           └──► S3 presigned URL
  │
  └── GET /status  ──► API Gateway ──► Lambda get_status ──► DynamoDB (scan)

S3 (input/job_id/file.csv)
  └──► [S3 Event] ──► Lambda trigger_pipeline ──► DynamoDB (PROCESSING)
                                               └──► Step Functions (start)

Step Functions: iot-pipeline
  ├── SubmitBatchJob (.sync:2) ──► AWS Batch / Fargate
  │     └── process_csv.py: download S3, conta rows, detecta anomalias, salva DynamoDB
  ├── SaveResults ──► Lambda save_results ──► DynamoDB (PROCESSED)
  └── Notify      ──► Lambda notify       ──► SNS
```
