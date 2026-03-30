FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=0 \
    ALLOW_DOCKER_HEADED_CAPTCHA=true \
    DISPLAY=:99 \
    XVFB_WHD=1920x1080x24

COPY requirements.txt ./

# 有头模式基础依赖：虚拟显示、窗口管理器。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        xvfb \
        fluxbox \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY . .
COPY docker/entrypoint.headed.sh /usr/local/bin/entrypoint.headed.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.headed.sh && chmod +x /usr/local/bin/entrypoint.headed.sh

EXPOSE 8000

CMD ["/usr/local/bin/entrypoint.headed.sh"]
