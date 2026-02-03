# Railway / Docker 배포용
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway가 PORT 환경 변수 주입. sh -c로 실행해 PORT 확실히 치환
EXPOSE 5000
CMD ["sh", "-c", "exec gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 2 --timeout 30 --keep-alive 5"]
