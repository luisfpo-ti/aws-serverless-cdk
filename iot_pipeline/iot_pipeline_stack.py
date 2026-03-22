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


class IotPipelineStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ─────────────────────────────────────────────────────────────
        # S3 Bucket  (recebe uploads de CSV via presigned URL)
        # ─────────────────────────────────────────────────────────────
        bucket = s3.Bucket(self, "IotDataBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            # CORS liberado para o browser fazer PUT direto no S3
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
        # DynamoDB  (status e resultados de cada job)
        # ─────────────────────────────────────────────────────────────
        table = dynamodb.Table(self, "FileProcessingJobs",
            table_name="FileProcessingJobs",
            partition_key=dynamodb.Attribute(
                name="job_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ─────────────────────────────────────────────────────────────
        # SNS Topic  (notificação ao concluir processamento)
        # ─────────────────────────────────────────────────────────────
        topic = sns.Topic(self, "Notifications",
            topic_name="iot-pipeline-notifications",
        )

        # ─────────────────────────────────────────────────────────────
        # VPC  (necessária para AWS Batch / Fargate)
        # Sem NAT Gateway para reduzir custo — subnets públicas com
        # assign_public_ip=True nas tasks
        # ─────────────────────────────────────────────────────────────
        vpc = ec2.Vpc(self, "BatchVpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                )
            ],
        )

        # ─────────────────────────────────────────────────────────────
        # Docker Image para o Batch Job  (ECR Asset gerenciado pelo CDK)
        # ─────────────────────────────────────────────────────────────
        batch_image = ecr_assets.DockerImageAsset(self, "ProcessorImage",
            directory=os.path.join(os.path.dirname(__file__), "../batch"),
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
        # ─────────────────────────────────────────────────────────────
        compute_env = batch.FargateComputeEnvironment(self, "ComputeEnv",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC,
            ),
        )

        job_queue = batch.JobQueue(self, "JobQueue",
            job_queue_name="iot-pipeline-queue",
            compute_environments=[
                batch.OrderedComputeEnvironment(
                    compute_environment=compute_env,
                    order=1,
                )
            ],
        )

        job_definition = batch.EcsJobDefinition(self, "JobDefinition",
            job_definition_name="iot-csv-processor",
            container=batch.EcsFargateContainerDefinition(self, "BatchContainer",
                image=ecs.ContainerImage.from_docker_image_asset(batch_image),
                cpu=1,
                memory=Size.mebibytes(2048),
                job_role=job_role,
                assign_public_ip=True,
                # Variáveis estáticas (deploy-time) — dinâmicas vêm do Step Functions
                environment={
                    "DYNAMODB_TABLE": table.table_name,
                },
                logging=ecs.LogDrivers.aws_logs(
                    stream_prefix="iot-batch",
                    log_retention=logs.RetentionDays.ONE_WEEK,
                ),
            ),
        )

        # ─────────────────────────────────────────────────────────────
        # Helper para criar Lambda functions com padrão consistente
        # ─────────────────────────────────────────────────────────────
        def make_lambda(name: str, handler_dir: str, timeout: int = 30, **env_vars):
            return lambda_.Function(self, name,
                function_name=name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler="app.lambda_handler",
                code=lambda_.Code.from_asset(f"functions/{handler_dir}"),
                timeout=Duration.seconds(timeout),
                tracing=lambda_.Tracing.ACTIVE,
                environment=env_vars,
                log_retention=logs.RetentionDays.ONE_WEEK,
            )

        # ─────────────────────────────────────────────────────────────
        # Lambda Functions
        # ─────────────────────────────────────────────────────────────

        # POST /upload → gera presigned URL + cria registro PENDING no DynamoDB
        presigned_url_fn = make_lambda("iot-presigned-url", "presigned_url",
            BUCKET_NAME=bucket.bucket_name,
            TABLE_NAME=table.table_name,
        )
        bucket.grant_put(presigned_url_fn)
        table.grant_write_data(presigned_url_fn)

        # GET /status → lista todos os jobs do DynamoDB
        get_status_fn = make_lambda("iot-get-status", "get_status",
            TABLE_NAME=table.table_name,
        )
        table.grant_read_data(get_status_fn)

        # S3 trigger → registra job como PROCESSING + inicia Step Functions
        trigger_pipeline_fn = make_lambda("iot-trigger-pipeline", "trigger_pipeline",
            TABLE_NAME=table.table_name,
            # STATE_MACHINE_ARN adicionado depois de criar a state machine
        )
        table.grant_write_data(trigger_pipeline_fn)

        # Chamado pelo Step Functions → atualiza status no DynamoDB (PROCESSED ou FAILED)
        save_results_fn = make_lambda("iot-save-results", "save_results",
            TABLE_NAME=table.table_name,
        )
        table.grant_write_data(save_results_fn)

        # Chamado pelo Step Functions → publica notificação no SNS
        notify_fn = make_lambda("iot-notify", "notify",
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
        )

        state_machine = sfn.StateMachine(self, "Pipeline",
            state_machine_name="iot-pipeline",
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

        # Permissões para Step Functions invocar Lambdas
        save_results_fn.grant_invoke(state_machine)
        notify_fn.grant_invoke(state_machine)

        # Permissões para Step Functions submeter e monitorar jobs do Batch
        state_machine.add_to_role_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "batch:SubmitJob",
                "batch:DescribeJobs",
                "batch:TerminateJob",
            ],
            resources=["*"],
        ))
        # Necessário para o padrão .sync:2 (usa EventBridge para detectar conclusão)
        state_machine.add_to_role_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "events:PutTargets",
                "events:PutRule",
                "events:DescribeRule",
            ],
            resources=[
                f"arn:aws:events:{self.region}:{self.account}:rule/StepFunctionsGetEventsForBatchJobsRule"
            ],
        ))

        # Conecta trigger_pipeline à state machine (depois de criada)
        state_machine.grant_start_execution(trigger_pipeline_fn)
        trigger_pipeline_fn.add_environment("STATE_MACHINE_ARN", state_machine.state_machine_arn)

        # ─────────────────────────────────────────────────────────────
        # S3 Event → dispara trigger_pipeline ao chegar arquivo em input/
        # ─────────────────────────────────────────────────────────────
        trigger_pipeline_fn.add_event_source(
            lambda_es.S3EventSource(bucket,
                events=[s3.EventType.OBJECT_CREATED],
                filters=[s3.NotificationKeyFilter(prefix="input/")],
            )
        )

        # ─────────────────────────────────────────────────────────────
        # API Gateway  (ponto de entrada do portal)
        # ─────────────────────────────────────────────────────────────
        api = apigw.RestApi(self, "IotPipelineApi",
            rest_api_name="iot-pipeline-api",
            description="API Gateway do Portal de Processamento IoT — Demo Serverless AWS",
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

        # POST /upload
        upload_resource = api.root.add_resource("upload")
        upload_resource.add_method(
            "POST",
            apigw.LambdaIntegration(presigned_url_fn),
            method_responses=[
                apigw.MethodResponse(status_code="200"),
                apigw.MethodResponse(status_code="400"),
                apigw.MethodResponse(status_code="500"),
            ],
        )

        # GET /status
        status_resource = api.root.add_resource("status")
        status_resource.add_method(
            "GET",
            apigw.LambdaIntegration(get_status_fn),
            method_responses=[
                apigw.MethodResponse(status_code="200"),
            ],
        )

        # ─────────────────────────────────────────────────────────────
        # CloudFormation Outputs  (usados pelo frontend via config.json)
        # ─────────────────────────────────────────────────────────────
        CfnOutput(self, "ApiUrl",
            value=api.url,
            description="URL base da API Gateway",
            export_name="IotPipelineApiUrl",
        )
        CfnOutput(self, "BucketName",
            value=bucket.bucket_name,
            description="Nome do bucket S3",
        )
        CfnOutput(self, "TableName",
            value=table.table_name,
            description="Nome da tabela DynamoDB",
        )
        CfnOutput(self, "TopicArn",
            value=topic.topic_arn,
            description="ARN do tópico SNS",
        )
        CfnOutput(self, "StateMachineArn",
            value=state_machine.state_machine_arn,
            description="ARN da Step Function",
        )
        CfnOutput(self, "JobQueueArn",
            value=job_queue.job_queue_arn,
            description="ARN da fila do AWS Batch",
        )
