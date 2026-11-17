FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home sticker \
    && mkdir /app/data && chown sticker:sticker /app/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sticker_bot ./sticker_bot
USER sticker
CMD ["python", "-m", "sticker_bot"]
