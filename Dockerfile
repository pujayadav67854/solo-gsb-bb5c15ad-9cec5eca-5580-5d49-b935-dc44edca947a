FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷贝应用代码
COPY app ./app

EXPOSE 8000

# 依赖安装与初始化均已在构建/启动阶段完成，无需额外命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
