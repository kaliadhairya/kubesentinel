#!/usr/bin/env bash
# ==============================================================================
# KubeSentinel KinD Local Chaos Cluster Setup Script
# ==============================================================================
set -euo pipefail

CLUSTER_NAME="kubesentinel-cluster"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "🛡️  Initializing KubeSentinel KinD Chaos Environment..."

# 1. Verify dependencies
if ! command -v kind &>/dev/null; then
    echo "❌ 'kind' is not installed. Please install KinD: https://kind.sigs.k8s.io/docs/user/quick-start/"
    exit 1
fi

if ! command -v kubectl &>/dev/null; then
    echo "❌ 'kubectl' is not installed. Please install kubectl: https://kubernetes.io/docs/tasks/tools/"
    exit 1
fi

# 2. Create KinD cluster if it doesn't already exist
if kind get clusters | grep -q "^${CLUSTER_NAME}$"; then
    echo "✅ Cluster '${CLUSTER_NAME}' already exists. Reusing existing cluster."
else
    echo "🚀 Provisioning 2-Node KinD cluster '${CLUSTER_NAME}'..."
    cat <<EOF | kind create cluster --name "${CLUSTER_NAME}" --config=-
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
- role: worker
EOF
fi

# 3. Set cluster context
kubectl cluster-info --context "kind-${CLUSTER_NAME}"

# 4. Create namespaces
echo "📁 Provisioning target application namespaces..."
kubectl apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: production
---
apiVersion: v1
kind: Namespace
metadata:
  name: staging
---
apiVersion: v1
kind: Namespace
metadata:
  name: monitoring
EOF

# 5. Apply Dual-ServiceAccount RBAC
echo "🔒 Applying Dual-ServiceAccount RBAC manifests..."
kubectl apply -f "${ROOT_DIR}/deploy/rbac/kubesentinel-observer.yaml"
kubectl apply -f "${ROOT_DIR}/deploy/rbac/kubesentinel-actuator.yaml"

# 6. Apply Chaos Workload Scenarios
echo "💥 Injecting Kubernetes Chaos Scenarios..."
kubectl apply -f "${ROOT_DIR}/deploy/chaos/checkout-oomkilled.yaml"
kubectl apply -f "${ROOT_DIR}/deploy/chaos/crashloop-missing-secret.yaml"
kubectl apply -f "${ROOT_DIR}/deploy/chaos/argo-application-tracking.yaml"

echo "=============================================================================="
echo "🎉 KinD Chaos Cluster is ready!"
echo "   Cluster Context: kind-${CLUSTER_NAME}"
echo "   Namespaces: production, staging, monitoring"
echo ""
echo "   Active Injected Chaos:"
echo "   1. production/checkout-service: Memory leak -> Exit Code 137 (OOMKilled)"
echo "   2. production/payment-service: Missing Secret -> CrashLoopBackOff"
echo "   3. production/cart-service: ArgoCD Tracking -> GitOps Self-Heal Guardrail"
echo ""
echo "   To point KubeSentinel at this cluster, set:"
echo "   export KUBECONFIG=\$(kind get kubeconfig-path --name ${CLUSTER_NAME} 2>/dev/null || echo ~/.kube/config)"
echo "   export K8S_MOCK_MODE=false"
echo "=============================================================================="
