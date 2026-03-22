#!/usr/bin/env python3
import os
import aws_cdk as cdk
from iot_pipeline.iot_pipeline_stack import IotPipelineStack

app = cdk.App()

IotPipelineStack(app, "IotPipelineStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
    description="IoT CSV File Processing Pipeline — Serverless na AWS (Demo)",
)

app.synth()
