# aws_small_ui blueprint: serverless UI + API on AWS.
#   HTTP API Gateway  ->  Lambda (Python, serves UI + API)  ->  DynamoDB (on-demand)
# Every resource carries iat:* tags so the Cost agent can meter it and find orphans.
# No fixed hourly cost: idle cost is ~$0, which is why it is the default blueprint.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.4" }
  }
  # Local state lives in the run workspace (.iat/runs/<run_id>/workspace/infra).
  # For a team setup, switch to an S3 backend with DynamoDB/S3 locking.
}

provider "aws" {
  region = var.region
  default_tags {
    tags = local.tags
  }
}

locals {
  name = "iat-${var.app_name}-${var.run_id}"
  tags = {
    "Project"        = var.app_name
    "iat:run-id"     = var.run_id
    "iat:managed-by" = "infra-agent-team"
    "iat:expires-at" = var.expires_at
    "iat:owner"      = var.owner
  }
}

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../app"
  output_path = "${path.module}/build/lambda.zip"
}

resource "aws_dynamodb_table" "submissions" {
  name         = local.name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery { enabled = false }
  deletion_protection_enabled = false # ephemeral by design; the Cost agent destroys it
}

resource "aws_iam_role" "lambda" {
  name = "${local.name}-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Least privilege: only this table and this function's log group.
resource "aws_iam_role_policy" "lambda" {
  name = "${local.name}-policy"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.submissions.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.lambda.arn}:*"
      }
    ]
  })
}

# Created explicitly (not by Lambda on first invoke) so that it is tagged, has a short
# retention, and is removed by terraform destroy instead of becoming an orphan.
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = 3
}

resource "aws_lambda_function" "app" {
  function_name    = local.name
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  memory_size      = var.lambda_memory_mb
  timeout          = 10

  environment {
    variables = {
      TABLE_NAME      = aws_dynamodb_table.submissions.name
      MAX_INPUT_CHARS = "5000"
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.lambda]
}

resource "aws_apigatewayv2_api" "http" {
  name          = local.name
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.app.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

# Throttling doubles as a cost guardrail: a public URL cannot be hammered into a big bill.
resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = var.throttle_burst
    throttling_rate_limit  = var.throttle_rate
  }
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.app.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}
