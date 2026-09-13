FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data

# 只读监控进程，无需暴露端口
CMD ["python", "main.py", "run"]
