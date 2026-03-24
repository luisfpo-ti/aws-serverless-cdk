# Conciliação Bancária — Guia de Deploy e Demo

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
cd bank-reconciliation

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
- S3 Bucket (recebe extratos CSV via presigned URL)
- DynamoDB table `ConciliacaoJobs`
- API Gateway REST API (`POST /upload` e `GET /status`)
- 5 Lambda functions (`recon-presigned-url`, `recon-get-status`, `recon-trigger-pipeline`, `recon-save-results`, `recon-notify`)
- AWS Batch (Fargate Compute Env + Job Queue `bank-reconciliation-queue` + Job Definition `bank-reconciliation-processor`)
- Step Functions state machine `bank-reconciliation-pipeline`
- SNS Topic para notificações
- VPC default da conta (sem criar nova infraestrutura de rede)
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

## 4. Demo: Gerar CSV de extrato bancário

```bash
# Gera CSV de 100k transações (~10 MB)
python3 - <<'EOF'
import csv, random, datetime, os

rows = 100_000
filename = f"extrato_{rows//1000}k.csv"

tipos = ['C', 'D']
descricoes = [
    'TEF RECEBIDO', 'TED ENVIADO', 'BOLETO PAGO', 'SALÁRIO DEPOSITADO',
    'TARIFA BANCÁRIA', 'PIX RECEBIDO', 'PIX ENVIADO', 'DOC ENVIADO',
    'RENDIMENTO POUPANÇA', 'DÉBITO AUTOMÁTICO', 'COMPRA CARTÃO',
]

with open(filename, 'w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f)
    writer.writerow(['data', 'descricao', 'valor', 'tipo', 'referencia'])
    for i in range(rows):
        tipo = random.choice(tipos)
        valor = round(random.uniform(10, 50000), 2)
        # ~3% de divergências: tipo C com valor negativo
        if random.random() < 0.03:
            valor = -valor
        writer.writerow([
            (datetime.date(2025, 1, 1) + datetime.timedelta(days=random.randint(0, 364))).isoformat(),
            random.choice(descricoes),
            valor,
            tipo,
            f"REF{i:08d}",
        ])

print(f"Gerado: {filename} ({os.path.getsize(filename)/1024/1024:.1f} MB)")
EOF
```

Para simular extrato grande (~500 MB, 5M de registros):
```bash
python3 -c "
import csv, random, sys, datetime
w = csv.writer(sys.stdout)
w.writerow(['data','descricao','valor','tipo','referencia'])
tipos = ['C','D']
for i in range(5_000_000):
    w.writerow([
        '2025-01-01', 'PIX', round(random.uniform(10,50000),2),
        random.choice(tipos), f'REF{i:08d}'
    ])
" > extrato_5M.csv
```

---

## 5. Demo: Upload via curl (sem portal)

```bash
# Lê o API URL do config.json
API_URL=$(python3 -c "import json; d=json.load(open('frontend/config.json')); print(list(d.values())[0]['ApiUrl'].rstrip('/'))")

# 1. Solicita presigned URL
RESPONSE=$(curl -s -X POST "$API_URL/upload" \
  -H "Content-Type: application/json" \
  -d '{"file_name": "extrato_100k.csv", "file_size": 10000000}')

echo $RESPONSE | python3 -m json.tool
JOB_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
UPLOAD_URL=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['upload_url'])")

# 2. Upload direto no S3 via presigned URL (sem passar pelo Lambda/API)
curl -X PUT "$UPLOAD_URL" \
  -H "Content-Type: text/csv" \
  --upload-file extrato_100k.csv \
  --progress-bar

# 3. Verifica status
curl -s "$API_URL/status" | python3 -m json.tool
```

---

## 6. Monitorar execução

```bash
# Verifica status via API
curl -s "$API_URL/status" | python3 -c "
import sys, json
jobs = json.load(sys.stdin)['jobs']
for j in jobs:
    saldo = j.get('saldo', '—')
    div   = j.get('divergencias', '—')
    print(f\"{j['job_id'][:8]} | {j['file_name']:<30} | {j['status']:<12} | saldo: {saldo} | div: {div}\")"

# Resultado no DynamoDB
aws dynamodb get-item \
  --table-name ConciliacaoJobs \
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
  ├── POST /upload ──► API Gateway ──► Lambda recon-presigned-url ──► DynamoDB (PENDING)
  │                                                                └──► S3 presigned URL
  │
  └── GET /status  ──► API Gateway ──► Lambda recon-get-status ──► DynamoDB (scan)

S3 (input/job_id/extrato.csv)
  └──► [S3 Event] ──► Lambda recon-trigger-pipeline ──► DynamoDB (PROCESSING)
                                                     └──► Step Functions (start)

Step Functions: bank-reconciliation-pipeline
  ├── SubmitBatchJob (.sync) ──► AWS Batch / Fargate
  │     └── process_csv.py: baixa CSV, concilia transações, calcula saldo/divergências
  ├── SaveResults ──► Lambda recon-save-results ──► DynamoDB (PROCESSED)
  └── Notify      ──► Lambda recon-notify       ──► SNS
```

---

## Deletar stack em ROLLBACK_COMPLETE (se necessário)

```bash
aws cloudformation delete-stack --stack-name BankReconciliationStack
aws cloudformation wait stack-delete-complete --stack-name BankReconciliationStack
cdk deploy --outputs-file frontend/config.json
```
