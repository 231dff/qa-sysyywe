$ErrorActionPreference = "Stop"

$ns = "agent-platform"
$backendImage = "qa-sysyywe:local"
$frontendImage = "qa-frontend:local"

Write-Host "==> [1/6] Building backend image..." -ForegroundColor Cyan
docker build -t $backendImage .

Write-Host "==> [2/6] Building frontend image..." -ForegroundColor Cyan
docker build -t $frontendImage --build-arg NEXT_PUBLIC_BACKEND_URL="" ./frontend

Write-Host "==> [3/6] Applying Backend K8s resources..." -ForegroundColor Cyan
kubectl apply -f k8s/configmaps/qa-sysyywe-config.yaml
kubectl apply -f k8s/quotas/qa-sysyywe-pvc.yaml
kubectl apply -f k8s/deployments/qa-sysyywe.yaml

Write-Host "==> [4/6] Applying Frontend K8s resources..." -ForegroundColor Cyan
# 请确保你的前端部署文件在这个路径，如果没有请修改此路径
kubectl apply -f k8s/deployments/qa-frontend.yaml

Write-Host "==> [5/6] Restarting deployments to pick up new images..." -ForegroundColor Cyan
# 使用 rollout restart 平滑重启，替代粗暴的 delete pod
kubectl rollout restart deployment/qa-sysyywe -n $ns
kubectl rollout restart deployment/qa-frontend -n $ns

Write-Host "==> [6/6] Waiting for rollouts to complete..." -ForegroundColor Cyan
kubectl rollout status deployment/qa-sysyywe -n $ns --timeout=180s
kubectl rollout status deployment/qa-frontend -n $ns --timeout=180s

Write-Host "`n==> Deployment Done! Current Pod status:" -ForegroundColor Green
kubectl get pods -n $ns