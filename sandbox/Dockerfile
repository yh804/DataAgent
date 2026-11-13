# 数据分析沙箱镜像：python:3.11-slim + 预装 pandas / numpy / matplotlib
# 必须在构建期装好依赖,因为运行时容器以 --network none 启动,无法在线安装
# 构建命令：docker build -t data-agent-sandbox:py311 sandbox/
FROM python:3.11-slim

# 安装中文字体(Noto Sans CJK)：matplotlib 图表含中文标题 / 轴标签时避免显示为方块
# --no-install-recommends 控制镜像体积,装完清理 apt 缓存
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "pandas==2.2.3" "numpy==2.1.3" "matplotlib==3.9.2"

# 约定：数据只读挂载到 /data,产物写入 /outputs
ENV DATA_DIR=/data
WORKDIR /outputs
