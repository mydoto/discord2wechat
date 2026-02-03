# 基于官方 Python 镜像
FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用
COPY discord_to_wecom.py .

# 可选：将 .env 放在镜像外部运行或在运行容器时通过 -e 注入环境变量
CMD ["python", "discord_to_wecom.py"]
