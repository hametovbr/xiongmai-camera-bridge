FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY xiongmai_bridge ./xiongmai_bridge
RUN pip install --no-cache-dir .

CMD ["python", "-m", "xiongmai_bridge.bridge"]
