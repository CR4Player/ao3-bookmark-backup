# syntax=docker/dockerfile:1
FROM --platform=$BUILDPLATFORM python:3.12-slim AS font-assets

# 在原生构建机预下载并校验自托管「霞鹜文楷（屏显 GB 版）」分片字体，避免 QEMU 慢速解压
RUN python - <<'EOF'
import base64, hashlib, io, re, tarfile, urllib.request
from pathlib import Path

url = "https://registry.npmjs.org/lxgw-wenkai-screen-webfont/-/lxgw-wenkai-screen-webfont-1.7.0.tgz"
expected_sha512 = "xvfF4V26Mb335xKg3uxljz+V2nq4F5ek0ku9pmyKgT0Dzo/d2rHi7WpnqgvM9mlXAcAKOrxrcDmUZiIs/ZQ9bQ=="
data = urllib.request.urlopen(url, timeout=60).read()
digest = base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
assert digest == expected_sha512, f"SHA512 mismatch: {digest}"

out = Path("/fonts/wenkai")
files_out = out / "files"
files_out.mkdir(parents=True, exist_ok=True)
count = 0
with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
    for m in tf.getmembers():
        if not m.isfile():
            continue
        if m.name == "package/lxgwwenkaigbscreen.css":
            (out / "style.css").write_bytes(tf.extractfile(m).read())
        elif m.name == "package/OFL.txt":
            (out / "OFL.txt").write_bytes(tf.extractfile(m).read())
        elif m.name.startswith("package/files/lxgwwenkaigbscreen-subset-") and m.name.endswith(".woff2"):
            fname = m.name.rsplit("/", 1)[-1]
            assert re.fullmatch(r"lxgwwenkaigbscreen-subset-\d+\.woff2", fname)
            (files_out / fname).write_bytes(tf.extractfile(m).read())
            count += 1

assert (out / "style.css").is_file() and (out / "style.css").stat().st_size > 10000
assert count == 97, f"Expected 97 woff2 subsets, got {count}"
EOF

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
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=font-assets /fonts /app/fonts
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
