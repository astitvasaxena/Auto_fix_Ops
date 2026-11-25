#!/usr/bin/env bash
# Idempotent. Run as root on the host: installs a single-node k3s cluster, puts the target app in
# it, and writes a least-privilege kubeconfig for AutoFixOps. Safe to re-run on every deploy:
# it never overwrites a live Deployment or chaos ConfigMap (remediation PRs manage those).
set -euo pipefail
# Run via sudo from the repo checkout: keep kubectl's cache in root's home, never inside the repo
# (a root-owned .kube in the build context makes `docker compose build` fail for the deploy user).
export HOME=/root
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd /
K3S_VERSION="${K3S_VERSION:-v1.31.4+k3s1}"
KUBE_DIR=/opt/autofixops/kube
NS=autofixops
APP="$ROOT/kubernetes_integration/target_app"
K="k3s kubectl"

if ! command -v k3s >/dev/null 2>&1; then
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" \
    INSTALL_K3S_EXEC="server --disable traefik --disable servicelb --disable metrics-server --tls-san host.docker.internal --write-kubeconfig-mode 600" sh -
fi
for _ in $(seq 1 60); do $K get nodes 2>/dev/null | grep -q " Ready" && break; sleep 3; done
$K get nodes | grep -q " Ready" || { echo "k3s node not Ready"; exit 1; }

# Target image: build with Docker, hand it to k3s' containerd (imagePullPolicy is Never).
docker build -q -t autofixops/target-app:latest "$APP" >/dev/null
docker save autofixops/target-app:latest | k3s ctr images import - >/dev/null

$K apply -f "$APP/namespace.yaml" >/dev/null
$K -n $NS get configmap target-app-chaos >/dev/null 2>&1 || $K apply -f "$APP/chaos-configmap.yaml"
$K -n $NS get deployment autofixops-target-app >/dev/null 2>&1 || $K apply -f "$APP/deployment.yaml"
$K apply -f "$APP/service-nodeport.yaml" >/dev/null
$K -n $NS rollout status deployment/autofixops-target-app --timeout=180s

# Least privilege: AutoFixOps may only touch the one namespace, and only what it needs.
cat <<YAML | $K apply -f - >/dev/null
apiVersion: v1
kind: ServiceAccount
metadata: {name: autofixops-operator, namespace: $NS}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: autofixops-operator, namespace: $NS}
rules:
- {apiGroups: ["apps"], resources: ["deployments"], verbs: ["get","list","watch","patch"]}
- {apiGroups: ["apps"], resources: ["replicasets"], verbs: ["get","list","watch"]}
- {apiGroups: [""], resources: ["pods"], verbs: ["get","list","watch","delete"]}
- {apiGroups: [""], resources: ["pods/log"], verbs: ["get"]}
- {apiGroups: [""], resources: ["events","services","endpoints"], verbs: ["get","list","watch"]}
- {apiGroups: [""], resources: ["configmaps"], verbs: ["get","list","patch"]}
- {apiGroups: [""], resources: ["services/proxy"], verbs: ["get"]}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: autofixops-operator, namespace: $NS}
subjects: [{kind: ServiceAccount, name: autofixops-operator, namespace: $NS}]
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: Role, name: autofixops-operator}
---
apiVersion: v1
kind: Secret
metadata:
  name: autofixops-operator-token
  namespace: $NS
  annotations: {kubernetes.io/service-account.name: autofixops-operator}
type: kubernetes.io/service-account-token
YAML
for _ in $(seq 1 30); do
  TOKEN=$($K -n $NS get secret autofixops-operator-token -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)
  [ -n "$TOKEN" ] && break; sleep 1
done
[ -n "$TOKEN" ] || { echo "service account token not issued"; exit 1; }
CA=$($K -n $NS get secret autofixops-operator-token -o jsonpath='{.data.ca\.crt}')
mkdir -p "$KUBE_DIR"
umask 022
cat > "$KUBE_DIR/config" <<KUBECONFIG
apiVersion: v1
kind: Config
clusters:
- name: autofixops
  cluster: {server: "https://host.docker.internal:6443", certificate-authority-data: "$CA"}
users:
- name: autofixops-operator
  user: {token: "$TOKEN"}
contexts:
- name: autofixops
  context: {cluster: autofixops, user: autofixops-operator, namespace: $NS}
current-context: autofixops
KUBECONFIG
chmod 644 "$KUBE_DIR/config"
echo "k3s ready; kubeconfig at $KUBE_DIR/config (context autofixops)"
