# Conciliação Bancária — AWS Serverless Demo

Portal serverless para upload e processamento de extratos bancários em CSV, com conciliação automática de transações, detecção de duplicatas e divergências.

## O que faz

- Recebe extratos bancários em CSV via upload direto para o S3 (presigned URL)
- Processa os registros em container Fargate via AWS Batch (sem limite de 15 min do Lambda)
- Detecta duplicatas (mesma referência mais de uma vez) e divergências (tipo/valor inconsistentes)
- Calcula saldo, total de créditos e débitos
- Notifica via SNS ao concluir
- Exibe o histórico de conciliações em tempo real no portal web

## Arquitetura

```
Browser
  │
  ├─ POST /upload ──► API Gateway ──► Lambda (presigned-url)
  │                                        │
  │                                        ├─ Cria job no DynamoDB (status: PENDING)
  │                                        └─ Retorna presigned URL
  │
  ├─ PUT (presigned URL) ──────────────────────────────► S3 (input/{job_id}/arquivo.csv)
  │                                                           │
  │                                                    S3 Event Notification
  │                                                           │
  │                                                           ▼
  │                                               Lambda (trigger-pipeline)
  │                                                    │
  │                                                    ├─ Atualiza DynamoDB (status: PROCESSING)
  │                                                    └─ Inicia Step Functions
  │                                                           │
  │                                              ┌────────────┴────────────┐
  │                                              ▼                         │
  │                                     AWS Batch (Fargate)           (em caso de erro)
  │                                     process_csv.py                     │
  │                                          │                             ▼
  │                                          ├─ Download CSV do S3    Lambda (save-results)
  │                                          ├─ Concilia registros    status: FAILED
  │                                          └─ Salva métricas no DynamoDB
  │                                                    │
  │                                                    ▼
  │                                          Lambda (save-results)
  │                                          status: PROCESSED
  │                                                    │
  │                                                    ▼
  │                                          Lambda (notify)
  │                                          Publica resumo no SNS
  │
  └─ GET /status ──► API Gateway ──► Lambda (get-status) ──► DynamoDB scan
       (poll 3s se há jobs ativos, 30s caso contrário)
```

## Fluxo detalhado

1. **Upload** — o browser solicita uma presigned URL via `POST /upload`. O Lambda cria o registro no DynamoDB com status `PENDING` e retorna a URL. O browser faz o `PUT` direto no S3, sem passar pelo Lambda.

2. **Trigger** — o S3 dispara um evento `ObjectCreated` para o Lambda `trigger-pipeline`, que atualiza o status para `PROCESSING` e inicia a execução do Step Functions.

3. **Processamento** — o Step Functions submete um job ao AWS Batch (Fargate). O container `process_csv.py` baixa o CSV do S3, processa todos os registros e salva as métricas diretamente no DynamoDB.

4. **Finalização** — após o Batch concluir, o Step Functions chama o Lambda `save-results` (status `PROCESSED`) e depois o Lambda `notify`, que publica o resumo no SNS.

5. **Erro** — qualquer falha no Batch é capturada pelo Step Functions e encaminhada ao `save-results` com status `FAILED`.

6. **Polling** — o portal faz `GET /status` a cada 3s enquanto há jobs ativos, reduzindo para 30s quando todos estão em estado final.

## Regras de conciliação

| Situação | Classificação |
|---|---|
| Mesma `referencia` aparece mais de uma vez | Duplicata |
| Tipo `C` com valor negativo | Divergência |
| Tipo `D` com valor positivo | Divergência |
| Tipo inválido (não C nem D) | Divergência |

## Estrutura do projeto

```
├── bank_reconciliation/
│   └── bank_reconciliation_stack.py  # CDK stack (toda a infra)
├── batch/
│   ├── Dockerfile                    # Container do job (linux/amd64)
│   ├── process_csv.py                # Lógica de conciliação
│   └── requirements.txt
├── functions/
│   ├── presigned_url/app.py          # Gera presigned URL + cria job
│   ├── trigger_pipeline/app.py       # Dispara Step Functions via S3 event
│   ├── save_results/app.py           # Atualiza status final no DynamoDB
│   ├── notify/app.py                 # Publica resumo no SNS
│   └── get_status/app.py             # Retorna lista de jobs para o portal
├── statemachine/
│   └── pipeline.asl.json             # Definição do Step Functions
├── frontend/
│   └── index.html                    # Portal web (single file, sem dependências)
└── samples/
    └── extrato_*.csv                 # Arquivos de exemplo para teste
```

## Formato do CSV

```csv
data,descricao,valor,tipo,referencia
2024-01-15,Pagamento Fornecedor,-1500.00,D,REF-001
2024-01-16,Recebimento Cliente,3200.00,C,REF-002
```

| Coluna | Descrição |
|---|---|
| `data` | Data da transação (YYYY-MM-DD) |
| `descricao` | Descrição do lançamento |
| `valor` | Valor em reais (positivo ou negativo) |
| `tipo` | `C` para crédito, `D` para débito |
| `referencia` | Código de referência / número do documento |

## Deploy

```bash
# Instalar dependências CDK
pip install -r requirements.txt

# Deploy na AWS
cdk deploy --profile <seu-perfil>
```

Após o deploy, configure a variável `API_URL` no Amplify (ou cole a URL do output do CDK no banner de setup do portal).

## Serviços AWS utilizados

- API Gateway — endpoints REST do portal
- Lambda — funções de orquestração e status
- S3 — armazenamento dos extratos
- AWS Batch (Fargate) — processamento dos CSVs sem limite de tempo
- Step Functions — orquestração do pipeline
- DynamoDB — estado e resultados dos jobs
- SNS — notificações de conclusão
- CloudWatch — logs e métricas
- ECR — imagem Docker do job Batch
- CDK Python — infraestrutura como código
