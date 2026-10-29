output "websocket_url" {
  description = "Connector add-on websocket_url; this is not the phone's external URL."
  value       = aws_apigatewayv2_stage.socket.invoke_url
}

output "phone_external_url" {
  description = "Companion app external address; restricted webhooks only, not a dashboard or login endpoint."
  value       = aws_apigatewayv2_api.http.api_endpoint
}

output "connector_parameter" {
  description = "SSM SecureString name. The secret itself is never read into Terraform state."
  value       = "/${var.name}/connector-key"
}
