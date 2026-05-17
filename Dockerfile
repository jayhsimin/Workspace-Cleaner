# Dockerfile — 相容 Render 免費方案 與 Hugging Face Spaces
# Render：自動注入 $PORT（通常為 10000+）
# HF Spaces：預設 PORT=7860

FROM python:3.11-slim

WORKDIR /app

# 先複製 requirements 利用 Docker layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製應用程式檔案
COPY main.py index.html ./

# 預設 7860（HF Spaces 標準），Render 會透過 $PORT 覆蓋
ENV PORT=7860
EXPOSE 7860

# sh -c 讓 $PORT 在執行時展開（而非 build 時）
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-7860}"]
