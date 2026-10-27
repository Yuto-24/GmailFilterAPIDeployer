FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --no-create-home app

COPY gmail_filter_deployer.py /app/gmail_filter_deployer.py
COPY gmail_message_reclassifier.py /app/gmail_message_reclassifier.py
USER app

ENTRYPOINT ["python", "/app/gmail_filter_deployer.py"]
CMD ["--help"]
