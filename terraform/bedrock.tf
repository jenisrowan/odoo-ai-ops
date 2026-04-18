resource "aws_bedrockagent_knowledge_base" "research_kb" {
  name     = "research-kb"
  role_arn = aws_iam_role.bedrock_kb_role.arn
  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = "arn:aws:bedrock:${data.aws_region.current.id}::foundation-model/amazon.titan-embed-text-v2:0"
    }
  }
  storage_configuration {
    type = "OPENSEARCH_SERVERLESS"
    opensearch_serverless_configuration {
      collection_arn    = aws_opensearchserverless_collection.bedrock_kb.arn
      vector_index_name = "bedrock-knowledge-base-default-index"
      field_mapping {
        vector_field   = "bedrock-knowledge-base-default-vector"
        text_field     = "AMAZON_BEDROCK_TEXT_CHUNK"
        metadata_field = "AMAZON_BEDROCK_METADATA"
      }
    }
  }
  depends_on = [
    aws_opensearchserverless_collection.bedrock_kb,
    aws_iam_role_policy.bedrock_kb_oss_access_policy,
    opensearch_index.bedrock_index
  ]
}

resource "aws_bedrockagent_data_source" "research_s3" {
  knowledge_base_id = aws_bedrockagent_knowledge_base.research_kb.id
  name              = "s3-document-vault"
  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn = aws_s3_bucket.company_research_vault.arn
    }
  }
}

resource "aws_bedrockagent_agent" "supervisor" {
  agent_name                  = "CustomerResearchSupervisor"
  agent_resource_role_arn     = aws_iam_role.bedrock_agent_role.arn
  foundation_model            = "apac.anthropic.claude-sonnet-4-20250514-v1:0"
  instruction                 = "You are a customer research supervisor. Your job is to compile a complete briefing by searching the web and the internal document vault to find all relevant information on a company. Once compiled, provide the comprehensive final report."
  idle_session_ttl_in_seconds = 1800
  prepare_agent               = true
}

# (Actions Groups)

resource "aws_bedrockagent_agent_action_group" "web_search" {
  agent_id           = aws_bedrockagent_agent.supervisor.id
  agent_version      = "DRAFT"
  action_group_name  = "WebSearch"
  action_group_state = "ENABLED"
  description        = "Use this action to search the public web via Tavily to find recent news and context."

  lifecycle {
    create_before_destroy = true
  }
  skip_resource_in_use_check = true
  action_group_executor {
    lambda = aws_lambda_function.librarian.arn
  }

  api_schema {
    payload = jsonencode({
      "openapi" = "3.0.0",
      "info" = {
        "title"       = "WebSearch API",
        "description" = "API for searching news and internal documents",
        "version"     = "1.0.0"
      },
      "paths" = {
        "/search" = {
          "post" = {
            "summary"     = "Search web for news",
            "description" = "Searches the public web for current news or context on a given query.",
            "operationId" = "SearchWeb",
            "parameters"  = [],
            "requestBody" = {
              "description" = "The search query payload",
              "required"    = true,
              "content" = {
                "application/json" = {
                  "schema" = {
                    "type"        = "object",
                    "description" = "Schema for search query",
                    "properties" = {
                      "query" = {
                        "type"        = "string",
                        "description" = "The specific text to search for."
                      }
                    },
                    "required" = ["query"]
                  }
                }
              }
            },
            "responses" = {
              "200" = {
                "description" = "Search results returned successfully",
                "content" = {
                  "application/json" = {
                    "schema" = {
                      "type"        = "object",
                      "description" = "Schema for search results",
                      "properties" = {
                        "search_result" = {
                          "type"        = "string",
                          "description" = "The summarized results from the web search."
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    })
  }
}



resource "aws_bedrockagent_agent_knowledge_base_association" "analyst" {
  agent_id             = aws_bedrockagent_agent.supervisor.id
  agent_version        = "DRAFT"
  knowledge_base_id    = aws_bedrockagent_knowledge_base.research_kb.id
  description          = "Use this to search internal PDF documents and 10-K filings."
  knowledge_base_state = "ENABLED"
}

resource "aws_bedrockagent_agent_alias" "prod" {
  agent_alias_name = "ProductionAlias"
  agent_id         = aws_bedrockagent_agent.supervisor.id
  description      = "Production alias for Odoo ECS integration"
}

# --- Logging and Monitoring ---

resource "aws_cloudwatch_log_group" "bedrock_agent_logs" {
  name              = "/aws/bedrock/agents/research-supervisor"
  retention_in_days = 7
}

resource "aws_bedrock_model_invocation_logging_configuration" "agent_logs" {
  depends_on = [
    aws_cloudwatch_log_group.bedrock_agent_logs,
    aws_iam_role_policy.bedrock_agent_policy
  ]

  logging_config {
    text_data_delivery_enabled = true
    cloudwatch_config {
      log_group_name = aws_cloudwatch_log_group.bedrock_agent_logs.name
      role_arn       = aws_iam_role.bedrock_agent_role.arn
    }
  }
}
