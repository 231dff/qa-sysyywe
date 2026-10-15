$ErrorActionPreference = "Stop"

$ns = "agent-platform"
$image = "qa-sysyywe:local"

Write-Host "==> [1/4] Building image..." -ForegroundColor Cyan
docker build -t $image .

Write-Host "==> [2/4] Applying K8s resources..." -ForegroundColor Cyan
kubectl apply -f k8s/configmaps/qa-sysyywe-config.yaml
kubectl apply -f k8s/quotas/qa-sysyywe-pvc.yaml
kubectl apply -f k8s/deployments/qa-sysyywe.yaml

Write-Host "==> [3/4] Restarting deployment to pick up new image..." -ForegroundColor Cyan
kubectl delete pod -n $ns -l app=qa-sysyywe --ignore-not-found

Write-Host "==> [4/4] Waiting for rollout..." -ForegroundColor Cyan
kubectl rollout status deployment/qa-sysyywe -n $ns --timeout=180s

Write-Host "`n==> Done. Pod status:" -ForegroundColor Green
kubectl get pods -n $ns -l app=qa-sysyywe