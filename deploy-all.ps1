$ErrorActionPreference = "Stop"

$ns = "agent-platform"
$backendImage = "qa-sysyywe:local"
$frontendImage = "qa-frontend:local"

Write-Host "==> [1/7] Building backend image..." -ForegroundColor Cyan
docker build -t $backendImage .

Write-Host "==> [2/7] Building frontend image..." -ForegroundColor Cyan
docker build -t $frontendImage --build-arg NEXT_PUBLIC_BACKEND_URL="" ./frontend

Write-Host "==> [3/7] Applying Redis resources..." -ForegroundColor Cyan
# ★ 新增这一步：应用新的 Redis Stack 配置
kubectl apply -f k8s/redis/redis.yaml

Write-Host "==> [4/7] Applying Backend K8s resources..." -ForegroundColor Cyan
kubectl apply -f k8s/configmaps/qa-sysyywe-config.yaml
kubectl apply -f k8s/quotas/qa-sysyywe-pvc.yaml
kubectl apply -f k8s/deployments/qa-sysyywe.yaml

Write-Host "==> [5/7] Applying Frontend K8s resources..." -ForegroundColor Cyan
kubectl apply -f k8s/deployments/qa-frontend.yaml

Write-Host "==> [6/7] Restarting deployments to pick up new images..." -ForegroundColor Cyan
# 重启前端、后端，以及 Redis（让 Redis 读取新镜像和参数）
kubectl rollout restart deployment/qa-sysyywe -n $ns
kubectl rollout restart deployment/qa-frontend -n $ns
kubectl rollout restart deployment/redis -n $ns  # ★ 新增：重启 Redis

Write-Host "==> [7/7] Waiting for rollouts to complete..." -ForegroundColor Cyan
kubectl rollout status deployment/redis -n $ns --timeout=180s
kubectl rollout status deployment/qa-sysyywe -n $ns --timeout=180s
kubectl rollout status deployment/qa-frontend -n $ns --timeout=180s

Write-Host "`n==> Deployment Done! Current Pod status:" -ForegroundColor Green
kubectl get pods -n $ns