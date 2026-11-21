# syntax=docker/dockerfile:1
FROM python:3.12-slim

# 容器内时区：RUN_AT 按这个时区解释
ARG TZ=Asia/Shanghai
ENV TZ=${TZ} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OUT_DIR=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime \
 && echo "${TZ}" > /etc/timezone

WORKDIR /app
COPY ao3_backup.py ao3_content.py ao3_diff.py ao3_store.py ao3_web.py ao3_web.css ao3_web.js ao3_reader.js /app/

# 非 root 运行；/data 是数据挂载点
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin ao3 \
 && mkdir -p /data \
 && touch /data/.volume-ready \
 && chown -R ao3:ao3 /app /data
USER ao3

VOLUME ["/data"]

# 最近一次成功运行超过 HEALTHCHECK_MAX_AGE_HOURS（默认 48）小时即判为不健康
HEALTHCHECK --interval=5m --timeout=20s --start-period=2m --retries=3 \
  CMD ["python", "/app/ao3_backup.py", "--healthcheck"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "/app/ao3_backup.py"]
