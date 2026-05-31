FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g @anthropic-ai/claude-code && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY claude_proxy.py /app/claude_proxy.py

EXPOSE 9100

HEALTHCHECK --interval=15s --timeout=6s --retries=3 --start-period=20s \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://localhost:9100/ready',timeout=4); sys.exit(0 if r.status==200 else 1)"

CMD ["python", "claude_proxy.py"]
