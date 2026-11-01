output "app_url" {
  value = aws_apigatewayv2_stage.default.invoke_url
}

output "function_name" {
  value = aws_lambda_function.app.function_name
}

output "table_name" {
  value = aws_dynamodb_table.submissions.name
}

output "api_id" {
  value = aws_apigatewayv2_api.http.id
}
