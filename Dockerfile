FROM python:3.12-slim

# 时区 + 编码 + Python 运行参数
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# 使用清华源（解决底层 perl 冲突）
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian/ trixie main non-free non-free-firmware contrib" > /etc/apt/sources.list && \
    echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian-security/ trixie-security main" >> /etc/apt/sources.list && \
    echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian/ trixie-updates main non-free non-free-firmware contrib" >> /etc/apt/sources.list

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# ★ 终极抗断流版：
# 1. 通过 config 配置全局镜像源
# 2. 将 timeout 设为 1000 秒
# 3. 移除了 --no-cache-dir，让下载的包留在 Docker 缓存里，断网重试不用从头再来
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn && \
    pip install --timeout 1000 -r requirements.txt

# 单独安装 sqlite-memory-mcp，并强制忽略它的依赖（防止破坏主环境）
RUN pip install --timeout 1000 --no-deps git+https://github.com/RMANOV/sqlite-memory-mcp.git

COPY . .

RUN mkdir -p /app/data/kb

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]