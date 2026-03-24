import os
import aws_cdk as cdk
from aws_cdk import (
    Stack, Duration, RemovalPolicy, Size, CfnOutput,
    aws_s3 as s3,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_es,
    aws_dynamodb as dynamodb,
    aws_apigateway as apigw,
    aws_stepfunctions as sfn,
    aws_batch as batch,
    aws_ecs as ecs,
    aws_ecr_assets as ecr_assets,
    aws_sns as sns,
    aws_iam as iam,
    aws_ec2 as ec2,
    aws_logs as logs,
)
from constructs import Construct


class BankReconciliationStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ─────────────────────────────────────────────────────────────
        # S3 Bucket  (recebe extratos bancários via presigned URL)
        # ─────────────────────────────────────────────────────────────
        bucket = s3.Bucket(self, "ExtratoBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            cors=[s3.CorsRule(
                allowed_methods=[
                    s3.HttpMethods.PUT,
                    s3.HttpMethods.GET,
                    s3.HttpMethods.HEAD,
                ],
                allowed_origins=["*"],
                allowed_headers=["*"],
                max_age=3000,
            )],
        )

        # ─────────────────────────────────────────────────────────────
        # DynamoDB  (status e resultados de cada conciliação)
        # ─────────────────────────────────────────────────────────────
        table = dynamodb.Table(self, "ConciliacaoJobs",
            table_name="ConciliacaoJobs",
            partition_key=dynamodb.Attribute(
                name="job_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ─────────────────────────────────────────────────────────────
        # SNS Topic  (notificação ao concluir conciliação)
        # ─────────────────────────────────────────────────────────────
        topic = sns.Topic(self, "Notifications",
            topic_name="bank-reconciliation-notifications",
        )

        # ─────────────────────────────────────────────────────────────
        # VPC  — usa a VPC default da conta (sem criar nova infra de rede)
        # ─────────────────────────────────────────────────────────────
        vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", is_default=True)

        # ─────────────────────────────────────────────────────────────
        # Docker Image para o Batch Job  (ECR Asset gerenciado pelo CDK)
        # ─────────────────────────────────────────────────────────────
        batch_image = ecr_assets.DockerImageAsset(self, "ReconciliacaoImage",
            directory=os.path.join(os.path.dirname(__file__), "../batch"),
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        # ─────────────────────────────────────────────────────────────
        # IAM Role para o Job do Batch  (acesso a S3 e DynamoDB)
        # ─────────────────────────────────────────────────────────────
        job_role = iam.Role(self, "BatchJobRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                ),
            ],
        )
        bucket.grant_read(job_role)
        table.grant_write_data(job_role)

        # ─────────────────────────────────────────────────────────────
        # AWS Batch  — Compute Environment (Fargate) + Job Queue + Job Def
        # Usa subnets públicas da VPC default + assign_public_ip para
        # alcançar ECR e S3 sem NAT Gateway
        # ─────────────────────────────────────────────────────────────
        compute_env = batch.FargateComputeEnvironment(self, "ComputeEnv",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC,
            ),
        )

        job_queue = batch.JobQueue(self, "JobQueue",
            job_queue_name="bank-reconciliation-queue",
            compute_environments=[
                batch.OrderedComputeEnvironment(
                    compute_environment=compute_env,
                    order=1,
                )
            ],
        )

        job_definition = batch.EcsJobDefinition(self, "JobDefinition",
            job_definition_name="bank-reconciliation-processor",
            container=batch.EcsFargateContainerDefinition(self, "BatchContainer",
                image=ecs.ContainerImage.from_docker_image_asset(batch_image),
                cpu=1,
                memory=Size.mebibytes(2048),
                job_role=job_role,
                assign_public_ip=True,
                environment={
                    "DYNAMODB_TABLE": table.table_name,
                },
                logging=ecs.LogDrivers.aws_logs(
                    stream_prefix="bank-recon-batch",
                ),
            ),
        )

        # ─────────────────────────────────────────────────────────────
        # Helper para criar Lambda functions com padrão consistente
        # Usa LogGroup explícito (log_retention está depreciado no CDK v2)
        # ─────────────────────────────────────────────────────────────
        def make_lambda(name: str, handler_dir: str, timeout: int = 30, **env_vars):
            log_group = logs.LogGroup(self, f"{name}-logs",
                log_group_name=f"/aws/lambda/{name}",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            )
            return lambda_.Function(self, name,
                function_name=name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler="app.lambda_handler",
                code=lambda_.Code.from_asset(f"functions/{handler_dir}"),
                timeout=Duration.seconds(timeout),
                tracing=lambda_.Tracing.ACTIVE,
                environment=env_vars,
                log_group=log_group,
            )

        # ─────────────────────────────────────────────────────────────
        # Lambda Functions
        # ─────────────────────────────────────────────────────────────

        presigned_url_fn = make_lambda("banking-presigned-url", "presigned_url",
            BUCKET_NAME=bucket.bucket_name,
            TABLE_NAME=table.table_name,
        )
        bucket.grant_put(presigned_url_fn)
        table.grant_write_data(presigned_url_fn)

        get_status_fn = make_lambda("banking-get-status", "get_status",
            TABLE_NAME=table.table_name,
        )
        table.grant_read_data(get_status_fn)

        trigger_pipeline_fn = make_lambda("banking-trigger-pipeline", "trigger_pipeline",
            TABLE_NAME=table.table_name,
        )
        table.grant_write_data(trigger_pipeline_fn)

        save_results_fn = make_lambda("banking-save-results", "save_results",
            TABLE_NAME=table.table_name,
        )
        table.grant_write_data(save_results_fn)

        notify_fn = make_lambda("banking-notify", "notify",
            TOPIC_ARN=topic.topic_arn,
            TABLE_NAME=table.table_name,
        )
        topic.grant_publish(notify_fn)
        table.grant_read_data(notify_fn)

        # ─────────────────────────────────────────────────────────────
        # Step Functions State Machine
        # ─────────────────────────────────────────────────────────────
        state_machine_log_group = logs.LogGroup(self, "StateMachineLogs",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        state_machine = sfn.StateMachine(self, "Pipeline",
            state_machine_name="bank-reconciliation-pipeline",
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_file(
                os.path.join(os.path.dirname(__file__), "../statemachine/pipeline.asl.json")
            ),
            definition_substitutions={
                "SaveResultsFunctionArn": save_results_fn.function_arn,
                "NotifyFunctionArn":      notify_fn.function_arn,
                "JobDefinitionArn":       job_definition.job_definition_arn,
                "JobQueueArn":            job_queue.job_queue_arn,
            },
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=state_machine_log_group,
                level=sfn.LogLevel.ALL,
                include_execution_data=True,
            ),
        )

        save_results_fn.grant_invoke(state_machine)
        notify_fn.grant_invoke(state_machine)

        state_machine.add_to_role_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["batch:SubmitJob", "batch:DescribeJobs", "batch:TerminateJob"],
            resources=["*"],
        ))
        state_machine.add_to_role_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["events:PutTargets", "events:PutRule", "events:DescribeRule"],
            resources=["*"],
        ))

        state_machine.grant_start_execution(trigger_pipeline_fn)
        trigger_pipeline_fn.add_environment("STATE_MACHINE_ARN", state_machine.state_machine_arn)

        # ─────────────────────────────────────────────────────────────
        # S3 Event → dispara trigger_pipeline ao chegar extrato em input/
        # ─────────────────────────────────────────────────────────────
        trigger_pipeline_fn.add_event_source(
            lambda_es.S3EventSource(bucket,
                events=[s3.EventType.OBJECT_CREATED],
                filters=[s3.NotificationKeyFilter(prefix="input/")],
            )
        )

        # ─────────────────────────────────────────────────────────────
        # API Gateway
        # ─────────────────────────────────────────────────────────────
        api = apigw.RestApi(self, "BankReconciliationApi",
            rest_api_name="bank-reconciliation-api",
            description="API Gateway do Portal de Conciliação Bancária — Demo Serverless AWS",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=apigw.Cors.ALL_METHODS,
                allow_headers=["Content-Type", "X-Api-Key", "Authorization"],
            ),
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                tracing_enabled=True,
                data_trace_enabled=False,
                logging_level=apigw.MethodLoggingLevel.INFO,
                metrics_enabled=True,
            ),
        )

        upload_resource = api.root.add_resource("upload")
        upload_resource.add_method("POST", apigw.LambdaIntegration(presigned_url_fn),
            method_responses=[
                apigw.MethodResponse(status_code="200"),
                apigw.MethodResponse(status_code="400"),
                apigw.MethodResponse(status_code="500"),
            ],
        )

        status_resource = api.root.add_resource("status")
        status_resource.add_method("GET", apigw.LambdaIntegration(get_status_fn),
            method_responses=[apigw.MethodResponse(status_code="200")],
        )

        # ─────────────────────────────────────────────────────────────
        # CloudFormation Outputs
        # ─────────────────────────────────────────────────────────────
        CfnOutput(self, "ApiUrl",           value=api.url,                          export_name="BankReconciliationApiUrl")
        CfnOutput(self, "BucketName",       value=bucket.bucket_name)
        CfnOutput(self, "TableName",        value=table.table_name)
        CfnOutput(self, "TopicArn",         value=topic.topic_arn)
        CfnOutput(self, "StateMachineArn",  value=state_machine.state_machine_arn)
        CfnOutput(self, "JobQueueArn",      value=job_queue.job_queue_arn)
