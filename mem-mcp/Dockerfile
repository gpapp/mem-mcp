# syntax=docker/dockerfile:1
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    apache2-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# BuildKit cache mount: wheels survive rebuilds and stay out of the image layer
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt --upgrade

COPY . .

# Set explicit timeouts for CPU-based Ollama calls
ENV HTTPX_TIMEOUT=600.0

CMD ["uv", "run", "server.py"]
#CMD ["fastmcp", "run", "server.py", "--with", "httpx", "--transport", "http", "--host", "0.0.0.0"]

