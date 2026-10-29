mock_provider "aws" {}

variables {
  aws_account_id = "123456789012"
}

run "phone_only" {
  command = plan

  assert {
    condition     = aws_apigatewayv2_route.phone.route_key == "POST /api/webhook/{webhook_id}" && length(aws_apigatewayv2_route.socket) == 3
    error_message = "Expose only the phone webhook and authenticated connector socket."
  }
  assert {
    condition     = aws_dynamodb_table.relay.billing_mode == "PAY_PER_REQUEST" && aws_dynamodb_table.relay.ttl[0].enabled && length(aws_dynamodb_table.relay.global_secondary_index) == 0
    error_message = "Transient phone data must expire; no account-linking index is needed."
  }
  assert {
    condition     = !contains(keys(aws_lambda_function.relay.environment[0].variables), "CLIENT_IDS") && !contains(keys(aws_lambda_function.relay.environment[0].variables), "OAUTH_PARAMETER")
    error_message = "Phone tracking must not depend on skill IDs or OAuth secrets."
  }
  assert {
    condition     = aws_lambda_permission.http.principal == "apigateway.amazonaws.com" && aws_lambda_permission.socket.principal == "apigateway.amazonaws.com"
    error_message = "Only the API gateways may invoke the relay."
  }
}

run "reject_invalid_account" {
  command = plan
  variables {
    aws_account_id = "wrong-account"
  }
  expect_failures = [var.aws_account_id]
}

run "reject_invalid_name" {
  command = plan
  variables {
    name = "../existing-deployment"
  }
  expect_failures = [var.name]
}
