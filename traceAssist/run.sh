#!/usr/bin/env bash
set -euo pipefail

echo "🗑  Deleting & restarting Minikube..."
minikube delete
minikube start --memory=7900 --cpus=4

echo "🔧 Configuring Docker to use Minikube..."
eval "$(minikube docker-env)"

echo "📦 Building images..."
docker build -t traceassist-backend:latest backend/
docker build -t traceassist-ai-agent:latest ai-agent/
docker build -t traceassist-frontend:latest frontend/

echo "📂 Ensuring namespaces exist..."
kubectl create namespace signoz      --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace traceassist --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace cert-manager --dry-run=client -o yaml | kubectl apply -f -

echo "🔐 Applying TraceAssist RBAC..."
kubectl -n traceassist apply -f k8s/traceassist-rbac.yaml

echo "🔐 Installing cert-manager..."
kubectl apply --validate=false \
  -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
kubectl -n cert-manager rollout status deployment cert-manager-webhook --timeout=180s

echo "🔧 Installing OpenTelemetry Operator (webhooks disabled)..."
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm repo update
helm upgrade --install \
  opentelemetry-operator open-telemetry/opentelemetry-operator \
  --namespace opentelemetry-operator-system --create-namespace \
  --wait --timeout=180s \
  --set admissionWebhooks.create=false \
  --set-string manager.env.ENABLE_WEBHOOKS=false

echo "📡 Applying Instrumentation CR..."
kubectl -n traceassist apply -f k8s/instrumentation.yaml

echo "🚀 Deploying TraceAssist services & OTEL Collector config..."
kubectl -n traceassist apply \
  -f k8s/backend-secret.yaml \
  -f k8s/ai-agent-secret.yaml \
  -f k8s/backend-deployment.yaml \
  -f k8s/backend-service.yaml \
  -f k8s/ai-agent-deployment.yaml \
  -f k8s/ai-agent-service.yaml \
  -f k8s/frontend-deployment.yaml \
  -f k8s/frontend-service.yaml \
  -f k8s/otel-collector-config.yaml \
  -f k8s/otel-collector-daemonset.yaml \
  -f k8s/otel-collector-infra.yaml

echo "📥 Installing Loki for log storage..."
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update
helm upgrade --install loki grafana/loki-stack \
  --namespace traceassist \
  --set promtail.enabled=false \
  --set grafana.enabled=false \
  --set loki.table_manager.retention_deletes_enabled=true \
  --set loki.table_manager.retention_period=168h

echo "🔄 Restarting backend and AI-Agent deployments..."
kubectl -n traceassist rollout restart deployment traceassist-backend
kubectl -n traceassist rollout restart deployment traceassist-ai-agent

echo
echo "✅ All components are up."
echo
echo "🔗 Frontend UI:"
echo "   kubectl -n traceassist port-forward svc/traceassist-frontend 5173:5173"
echo
echo "🔗 Backend API:"
echo "   kubectl -n traceassist port-forward svc/traceassist-backend 8000:8000"
echo
