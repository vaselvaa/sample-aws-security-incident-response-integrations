from os import path
from aws_cdk import (
    CfnOutput,
    CfnParameter,
    Duration,
    Stack,
    aws_events,
    aws_events_targets,
    aws_iam,
    aws_lambda,
    aws_lambda_python_alpha as py_lambda,
    aws_ssm,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
)
from cdk_nag import NagSuppressions
from constructs import Construct
from .constants import JIRA_AWS_ACCOUNT_ID, JIRA_EVENT_SOURCE, SECURITY_IR_EVENT_SOURCE

class AwsSecurityIncidentResponseJiraIntegrationStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, common_stack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        
        if(common_stack is None):
            raise ValueError("Common stack cannot be null")
        
        # Reference common resources
        table = common_stack.table
        event_bus = common_stack.event_bus
        event_bus_logger = common_stack.event_bus_logger
        domain_layer = common_stack.domain_layer
        mappers_layer = common_stack.mappers_layer
        wrappers_layer = common_stack.wrappers_layer
        log_level_param = common_stack.log_level_param
        
        """
        cdk for setting Jira Client parameters
        """
        # Create Jira client parameters
        jira_email_param = CfnParameter(
            self,
            "jiraEmail",
            type="String",
            description="The email address that will be used with the Jira API.",
            no_echo=True,
        )

        # Store Jira URL CFN parameter
        jira_url_param = CfnParameter(
            self, 
            "jiraUrl", 
            type="String", 
            description="The URL of the Jira API.",
        )

        # Store Jira token CFN parameter
        jira_token_param = CfnParameter(
            self,
            "jiraToken",
            type="String",
            description="The API token that will be used with the Jira API.",
            no_echo=True,
        )
        
        # Create SSM parameters
        jira_token_ssm_param = aws_ssm.StringParameter(
            self,
            "JiraTokenSecret",
            string_value=jira_token_param.value_as_string,
        )

        jira_email_ssm = aws_ssm.StringParameter(
            self,
            "jiraEmailSSM",
            parameter_name="/SecurityIncidentResponse/jiraEmail",
            string_value=jira_email_param.value_as_string,
            description="Jira email",
        )

        jira_url_ssm = aws_ssm.StringParameter(
            self,
            "jiraUrlSSM",
            parameter_name="/SecurityIncidentResponse/jiraUrl",
            string_value=jira_url_param.value_as_string,
            description="Jira URL",
        )
        
        """
        cdk for assets/jira_notifications_handler
        """
        # Create Jira notifications handler and related resources
        jira_notifications_handler_role = aws_iam.Role(
            self,
            "JiraNotificationsHandlerRole",
            assumed_by=aws_iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Custom role for Jira Notifications Handler Lambda function"
        )
        
        # Add custom policy for CloudWatch Logs permissions
        jira_notifications_handler_role.add_to_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents"
                ],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/lambda/*"
                ]
            )
        )
        # Create Lambda function for Jira Notifications handler with custom role
        jira_notifications_handler = py_lambda.PythonFunction(
            self,
            "JiraNotificationsHandler",
            entry=path.join(path.dirname(__file__), "..", "assets/jira_notifications_handler"),
            runtime=aws_lambda.Runtime.PYTHON_3_13,
            layers=[domain_layer, mappers_layer, wrappers_layer],
            environment={
                "EVENT_BUS_NAME": event_bus.event_bus_name,
                "JIRA_EMAIL": "/SecurityIncidentResponse/jiraEmail",
                "JIRA_URL": "/SecurityIncidentResponse/jiraUrl",
                "INCIDENTS_TABLE_NAME": table.table_name,
                "JIRA_TOKEN_PARAM": jira_token_ssm_param.parameter_name,
                "EVENT_SOURCE": JIRA_EVENT_SOURCE,
                "LOG_LEVEL": log_level_param.value_as_string
            },
            role=jira_notifications_handler_role
        )
        
        # Create SNS topic for JIRA notifications
        jira_notifications_topic = sns.Topic(
            self,
            "JiraNotificationsTopic",
            display_name="Jira Notifications Topic"
        )

        # Add Lambda subscription to the JIRA notifications SNS topic
        jira_notifications_topic.add_subscription(
            subscriptions.LambdaSubscription(
                jira_notifications_handler
            )
        )

        # Create a topic policy for the JIRA notifications SNS topic
        jira_notifications_topic_policy = sns.TopicPolicy(
            self,
            "JiraNotificationsTopicPolicy",
            topics=[jira_notifications_topic],
        )

        # Add policy statements to the JIRA notifications SNS topic
        jira_notifications_topic_policy.document.add_statements(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                principals=[aws_iam.ServicePrincipal("events.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[jira_notifications_topic.topic_arn],
                conditions={
                    "StringEquals": {
                        "AWS:SourceAccount": self.account
                    }
                }
            )
        )

        # Add policy to let JIRA IAM principal publish events to SNS topic
        jira_notifications_topic_policy.document.add_statements(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                principals=[aws_iam.AccountPrincipal(JIRA_AWS_ACCOUNT_ID)],
                actions=["SNS:Publish"],
                resources=[jira_notifications_topic.topic_arn]
            )
        )
        
        # Grant the SNS topic permission to invoke the Lambda function
        jira_notifications_handler.grant_invoke(
            aws_iam.ServicePrincipal("sns.amazonaws.com")
        )
        
        # Add permissions to the role directly
        jira_notifications_handler.add_to_role_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                actions=[
                    "security-ir:GetCase",
                    "security-ir:UpdateCase",
                    "security-ir:ListCases",
                    "security-ir:CreateCase",
                    "security-ir:ListComments",
                    "events:PutEvents",
                    "events:DescribeRule",
                    "events:ListRules",
                    "lambda:GetFunctionConfiguration",
                    "lambda:UpdateFunctionConfiguration",
                ],
                resources=["*"]
            )
        )
        
        # Add specific permission for the custom event bus
        jira_notifications_handler.add_to_role_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                actions=["events:PutEvents"],
                resources=[event_bus.event_bus_arn],
            )
        )
        
        # Allow adding SSM values
        jira_notifications_handler.add_to_role_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                actions=["ssm:GetParameter", "ssm:PutParameter"],
                resources=["*"],
            )
        )
        
        # Add suppressions for IAM5 findings related to wildcard resources
        NagSuppressions.add_resource_suppressions(
            jira_notifications_handler,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Wildcard resources are required for security-ir, events, lambda, and SSM actions",
                    "applies_to": ["Resource::*"]
                }
            ],
            True
        )
        
        # Add a specific rule for Jira notification events
        jira_notifications_rule = aws_events.Rule(
            self,
            "JiraNotificationsRule",
            description="Rule to capture events from Jira notifications handler",
            event_pattern=aws_events.EventPattern(
                source=[JIRA_EVENT_SOURCE]
            ),
            event_bus=event_bus,
        )

        # Use the same log group as the event bus logger
        jira_notifications_target = aws_events_targets.CloudWatchLogGroup(
            log_group=event_bus_logger.log_group
        )
        jira_notifications_rule.add_target(jira_notifications_target)

        # Grant specific DynamoDB permissions instead of full access
        table.grant_read_write_data(jira_notifications_handler)
        
        """
        cdk for assets/jira_client
        """
        # Create a custom role for the Jira Client Lambda function
        jira_client_role = aws_iam.Role(
            self,
            "SecurityIncidentResponseJiraClientRole",
            assumed_by=aws_iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Custom role for Security Incident Response Jira Client Lambda function"
        )
        
        # Add custom policy for CloudWatch Logs permissions
        jira_client_role.add_to_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents"
                ],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/lambda/*"
                ]
            )
        )
        
        # create Lambda function for Jira with custom role
        jira_client = py_lambda.PythonFunction(
            self,
            "SecurityIncidentResponseJiraClient",
            entry=path.join(path.dirname(__file__), "..", "assets/jira_client"),
            runtime=aws_lambda.Runtime.PYTHON_3_13,
            timeout=Duration.minutes(15),
            layers=[domain_layer, mappers_layer, wrappers_layer],
            environment={
                "JIRA_EMAIL": "/SecurityIncidentResponse/jiraEmail",
                "JIRA_URL": "/SecurityIncidentResponse/jiraUrl",
                "INCIDENTS_TABLE_NAME": table.table_name,
                "JIRA_TOKEN_PARAM": jira_token_ssm_param.parameter_name,
                "EVENT_SOURCE": SECURITY_IR_EVENT_SOURCE,
                "LOG_LEVEL": log_level_param.value_as_string
            },
            role=jira_client_role
        )
        
        # create Event Bridge rule for Jira Client Lambda function
        jira_client_rule = aws_events.Rule(
            self,
            "jira-client-rule",
            description="Rule to send all events to Jira Lambda function",
            event_pattern=aws_events.EventPattern(source=[SECURITY_IR_EVENT_SOURCE]),
            event_bus=event_bus,
        )
        
        # Add target
        jira_client_target = aws_events_targets.LambdaFunction(jira_client)
        jira_client_rule.add_target(jira_client_target)
        
        # grant permissions to DynamoDB table and security-ir
        jira_client_role.add_to_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                actions=[
                    "security-ir:GetCaseAttachmentDownloadUrl",
                    "security-ir:ListComments"
                ],
                resources=["*"],
            )
        )

        # allow adding SSM values
        jira_client_role.add_to_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                actions=["ssm:GetParameter", "ssm:PutParameter"],
                resources=["*"],
            )
        )

        # Grant specific DynamoDB permissions instead of full access
        table.grant_read_write_data(jira_client_role)
        
        # Add suppressions for IAM5 findings related to wildcard resources
        NagSuppressions.add_resource_suppressions(
            jira_client_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Wildcard resources are required for security-ir and SSM actions",
                    "applies_to": ["Resource::*"]
                }
            ],
            True
        )
        
        """
        cdk for assets/security_ir_client
        """
        # Create a custom role for the Security IR Client Lambda function
        security_ir_client_role = aws_iam.Role(
            self,
            "SecurityIncidentResponseClientRole",
            assumed_by=aws_iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Custom role for Security Incident Response Client Lambda function"
        )
        
        # Add custom policy for CloudWatch Logs permissions
        security_ir_client_role.add_to_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents"
                ],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/lambda/*"
                ]
            )
        )
        
        security_ir_client = py_lambda.PythonFunction(
            self,
            "SecurityIncidentResponseClient",
            entry=path.join(path.dirname(__file__), "..", "assets/security_ir_client"),
            runtime=aws_lambda.Runtime.PYTHON_3_13,
            timeout=Duration.minutes(15),
            layers=[domain_layer, mappers_layer, wrappers_layer],
            environment={
                "EVENT_SOURCE": JIRA_EVENT_SOURCE,
                "INCIDENTS_TABLE_NAME": table.table_name
            },
            role=security_ir_client_role
        )
        
        # create Event Bridge rule for Security Incident Response Client Lambda function
        security_ir_client_rule = aws_events.Rule(
            self,
            "security-ir-client-rule",
            description="Rule to send all events to Security Incident Response Client lambda function",
            event_pattern=aws_events.EventPattern(source=[JIRA_EVENT_SOURCE]),
            event_bus=event_bus,
        )
        security_ir_client_rule.add_target(aws_events_targets.LambdaFunction(security_ir_client))
        
        # Add permissions for Security IR API
        security_ir_client.add_to_role_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                actions=[
                    "dynamodb:PutItem", 
                    "dynamodb:GetItem", 
                    "dynamodb:UpdateItem",
                    "security-ir:UpdateCase",
                    "security-ir:CreateCaseComment",
                    "security-ir:UpdateCaseComment",
                    "security-ir:UpdateCaseStatus",
                    "security-ir:ListComments",
                    "security-ir:GetCase",
                    "security-ir:CreateCase",
                    "security-ir:CloseCase",
                    "security-ir:GetCaseAttachmentUploadUrl"
                ],
                resources=["*"],
            )
        )
        
        # Grant specific DynamoDB permissions instead of full access
        table.grant_read_write_data(security_ir_client)
        
        # Add suppressions for IAM5 findings related to wildcard resources
        NagSuppressions.add_resource_suppressions(
            security_ir_client,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Wildcard resources are required for DynamoDB actions",
                    "applies_to": ["Resource::*"]
                }
            ],
            True
        )
        
        # Add stack-level suppression
        NagSuppressions.add_stack_suppressions(
            self, [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "Built-in LogRetention Lambda role requires AWSLambdaBasicExecutionRole managed policy"
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Built-in LogRetention Lambda needs these permissions to manage log retention"
                },
                {
                    "id": "AwsSolutions-SQS3",
                    "reason": "SQS is used as DLQ"
                },
                {
                    "id": "AwsSolutions-SNS3",
                    "reason": "Jira Notifications SNS Topic requires encryption disabled"
                },
                {
                    "id": "AwsSolutions-L1",
                    "reason": "CDK-generated Lambda functions may use older runtimes which we cannot directly control"
                }
            ]
        )
        
        """
        cdk to output the generated name of CFN resources 
        """
        # Output Jira client ARN
        CfnOutput(
            self,
            "JiraClientLambdaArn",
            value=jira_client.function_arn,
            description="Jira Client Lambda Function ARN",
        )
        
        CfnOutput(
            self,
            "SecurityIRClientLambdaArn",
            value=security_ir_client.function_arn,
            description="Security Incident Response Client Lambda Function ARN",
        )
        
        # Output Jira notifications handler log group info
        CfnOutput(
            self,
            "JiraNotificationsHandlerLambdaLogGroup",
            value=jira_notifications_handler.log_group.log_group_name,
            description="Jira Notifications Handler Lambda CloudWatch Logs Group Name"
        )

        # Output the CloudWatch Logs URL for the jira-notifications-handler lambda function
        CfnOutput(
            self,
            "JiraNotificationsHandlerLambdaLogGroupUrl",
            value=f"https://console.aws.amazon.com/cloudwatch/home?region={Stack.of(self).region}#logsV2:log-groups/log-group/{jira_notifications_handler.log_group.log_group_name}",
            description="Jira Notifications Handler Lambda CloudWatch Logs URL"
        )