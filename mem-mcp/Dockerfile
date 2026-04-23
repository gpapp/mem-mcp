FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt --upgrade

COPY . .

# Set explicit timeouts for CPU-based Ollama calls
ENV HTTPX_TIMEOUT=600.0

CMD ["uv", "run", "server.py"]
#CMD ["fastmcp", "run", "server.py", "--with", "httpx", "--transport", "http", "--host", "0.0.0.0"]

