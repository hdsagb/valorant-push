# 服务器版镜像：python:3.12-slim + CJK 字体 + 时区；非 root 运行
FROM python:3.12-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VAL_SERVER_DATA_DIR=/data

# tzdata：slim 无系统时区库，models.zone() 的 ZoneInfo 需要（M4 计划 §0 决策 6）
# fonts-noto-cjk：渲染中文字体，Debian 安装路径即 render.py 候选路径（§0 决策 7）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && ln -fs /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

# 非 root（uid 1000）；预建 /data 并赋属主——命名卷首次挂载继承此属主（§0 决策 5）
RUN useradd --uid 1000 --create-home app \
    && mkdir -p /data && chown app:app /data

WORKDIR /srv
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY push_server/ ./push_server/

USER app
VOLUME /data
EXPOSE 8787

# /healthz 免鉴权（M3）；slim 无 curl/wget，用 stdlib urllib；端口兼容自定义
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.getenv('VAL_SERVER_WEB_PORT','8787'), timeout=4)"]

# serve 模式 = aiohttp + scheduler.loop() 同一事件循环（M3 Task 6）
CMD ["python", "-m", "push_server"]
