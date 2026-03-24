#!/usr/bin/env python3
import os
import aws_cdk as cdk
from bank_reconciliation.bank_reconciliation_stack import BankReconciliationStack

app = cdk.App()

BankReconciliationStack(app, "BankReconciliationStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
    description="Pipeline de Conciliação Bancária — Serverless na AWS (Demo)",
)

app.synth()
