FROM python:3.12-slim

# 时区 + 编码 + Python 运行参数 + 强制离线模式
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_OFFLINE=1 \
    HF_HOME=/app/models

# 使用清华源（解决底层 perl 冲突）
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian/ trixie main non-free non-free-firmware contrib" > /etc/apt/sources.list && \
    echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian-security/ trixie-security main" >> /etc/apt/sources.list && \
    echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian/ trixie-updates main non-free non-free-firmware contrib" >> /etc/apt/sources.list

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# 配置 pip 镜像
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn

# ★ 核心修改：强制先安装 CPU 版的 PyTorch！
# 这能阻止 pip 下载几 GB 的 NVIDIA CUDA 驱动包，大幅缩减构建时间和镜像体积
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

# 安装 requirements 中的其余依赖
RUN pip install --timeout 1000 -r requirements.txt

# ★ 直接把本地模型拷进镜像里
COPY ./models/bge-m3 /app/models/bge-m3

# 单独安装 sqlite-memory-mcp
RUN pip install --timeout 1000 --no-deps git+https://github.com/RMANOV/sqlite-memory-mcp.git

COPY . .

RUN mkdir -p /app/data/kb

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]