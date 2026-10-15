variable "IMAGE_PREFIX" { default = "ghcr.io/redpanda321/myagent.ai" }
variable "DEPLOY_SHA" { default = "dev" }
variable "DEPLOY_ENVIRONMENT" { default = "staging" }
variable "PUBLIC_ORIGIN" { default = "https://staging.hangent.com" }

group "default" {
  targets = ["api", "user", "recommendation", "embedding", "job", "vector", "chatbot", "frontend"]
}

target "api" {
  context = "./backend/api_gateway"
  tags = ["${IMAGE_PREFIX}/hanggent-career-compass-api:${DEPLOY_SHA}-${DEPLOY_ENVIRONMENT}"]
}

target "user" {
  context = "./backend/user_service"
  tags = ["${IMAGE_PREFIX}/hanggent-career-compass-user:${DEPLOY_SHA}-${DEPLOY_ENVIRONMENT}"]
}

target "recommendation" {
  context = "./backend/recommendation_service"
  tags = ["${IMAGE_PREFIX}/hanggent-career-compass-recommendation:${DEPLOY_SHA}-${DEPLOY_ENVIRONMENT}"]
}

target "embedding" {
  context = "./backend/embedding_service"
  tags = ["${IMAGE_PREFIX}/hanggent-career-compass-embedding:${DEPLOY_SHA}-${DEPLOY_ENVIRONMENT}"]
}

target "job" {
  context = "./backend/job_service"
  tags = ["${IMAGE_PREFIX}/hanggent-career-compass-job:${DEPLOY_SHA}-${DEPLOY_ENVIRONMENT}"]
}

target "vector" {
  context = "./backend/vectordb_service"
  tags = ["${IMAGE_PREFIX}/hanggent-career-compass-vector:${DEPLOY_SHA}-${DEPLOY_ENVIRONMENT}"]
}

target "chatbot" {
  context = "./backend/chatbot"
  tags = ["${IMAGE_PREFIX}/hanggent-career-compass-chatbot:${DEPLOY_SHA}-${DEPLOY_ENVIRONMENT}"]
}

target "frontend" {
  context = "./frontend/app"
  tags = ["${IMAGE_PREFIX}/hanggent-career-compass-frontend:${DEPLOY_SHA}-${DEPLOY_ENVIRONMENT}"]
  args = {
    VITE_API_GATEWAY_URL = "${PUBLIC_ORIGIN}/career-compass/api"
    VITE_BASE_PATH = "/career-compass/"
  }
}
