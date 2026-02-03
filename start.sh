#!/bin/sh

# Railway/Nixpacks entrypoint.
# Railway는 startCommand 문자열을 셸 없이 실행하므로 `$PORT` 치환이 되지 않아 Gunicorn이 종료되곤 한다.
# 이 스크립트에서 PORT 기본값을 지정하고 exec로 Gunicorn을 실행한다.

PORT_VALUE="${PORT:-5000}"

exec gunicorn app:app \
  --bind "0.0.0.0:${PORT_VALUE}" \
  --workers 2 \
  --threads 2 \
  --timeout 30 \
  --keep-alive 5
