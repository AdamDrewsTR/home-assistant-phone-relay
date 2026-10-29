data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "archive_file" "relay" {
  type        = "zip"
  output_path = "${path.module}/.build/relay.zip"
  source {
    content  = file("${path.module}/lambda/relay.py")
    filename = "relay.py"
  }
}

resource "aws_dynamodb_table" "relay" {
  name         = "${var.name}-relay"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  attribute {
    name = "pk"
    type = "S"
  }
  ttl {
    attribute_name = "expires"
    enabled        = true
  }
  server_side_encryption {
    enabled = true
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cloudwatch_log_group" "relay" {
  name              = "/aws/lambda/${var.name}-relay"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "relay" {
  name = "${var.name}-relay"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRole",
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "relay" {
  role = aws_iam_role.relay.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
        Resource = aws_dynamodb_table.relay.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.relay.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.name}/connector-key"
      },
      {
        Effect   = "Allow"
        Action   = ["execute-api:ManageConnections"]
        Resource = "${aws_apigatewayv2_api.socket.execution_arn}/connector/POST/@connections/*"
      }
    ]
  })
}

resource "aws_lambda_function" "relay" {
  function_name    = "${var.name}-relay"
  role             = aws_iam_role.relay.arn
  filename         = data.archive_file.relay.output_path
  source_code_hash = data.archive_file.relay.output_base64sha256
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "relay.handler"
  timeout          = 7
  memory_size      = 512
  environment {
    variables = {
      TABLE               = aws_dynamodb_table.relay.name
      SOCKET_ENDPOINT     = replace(aws_apigatewayv2_stage.socket.invoke_url, "wss://", "https://")
      CONNECTOR_PARAMETER = "/${var.name}/connector-key"
      HTTP_URL            = aws_apigatewayv2_api.http.api_endpoint
    }
  }
  depends_on = [aws_iam_role_policy.relay]
}

resource "aws_apigatewayv2_api" "socket" {
  name                       = "${var.name}-connector"
  protocol_type              = "WEBSOCKET"
  route_selection_expression = "$request.body.action"
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_apigatewayv2_integration" "socket" {
  api_id             = aws_apigatewayv2_api.socket.id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.relay.invoke_arn
  integration_method = "POST"
}

resource "aws_apigatewayv2_route" "socket" {
  for_each  = toset(["$connect", "$disconnect", "$default"])
  api_id    = aws_apigatewayv2_api.socket.id
  route_key = each.key
  target    = "integrations/${aws_apigatewayv2_integration.socket.id}"
}

resource "aws_apigatewayv2_stage" "socket" {
  api_id      = aws_apigatewayv2_api.socket.id
  name        = "connector"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 5
  }
}

resource "aws_apigatewayv2_api" "http" {
  name          = "${var.name}-phones"
  protocol_type = "HTTP"
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_apigatewayv2_integration" "http" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.relay.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "phone" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "POST /api/webhook/{webhook_id}"
  target    = "integrations/${aws_apigatewayv2_integration.http.id}"
}

resource "aws_apigatewayv2_stage" "http" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 2
  }
}

resource "aws_lambda_permission" "socket" {
  statement_id  = "ConnectorGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.relay.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.socket.execution_arn}/*"
}

resource "aws_lambda_permission" "http" {
  statement_id  = "PhoneGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.relay.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*"
}
