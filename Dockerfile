FROM python:3.12-slim

# 时区 + 编码 + Python 运行参数
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_NO_CACHE_DIR=1

# 基础工具（★ 增加了 git，用于从 GitHub 拉取 sqlite-memory-mcp）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用 layer cache）
COPY requirements.txt .
# ★ 使用清华镜像源加速并避免 SSL 报错
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --trusted-host pypi.tuna.tsinghua.edu.cn

# 再拷代码
COPY . .

# 建好数据目录（会被 PVC 覆盖）
RUN mkdir -p /app/data/kb

# 非 root 用户运行
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]