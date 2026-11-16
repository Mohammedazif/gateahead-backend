FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV GATEAHEAD_DATA=/var/lib/gateahead PYTHONUNBUFFERED=1
RUN mkdir -p /var/lib/gateahead
EXPOSE 8000
CMD ["python", "-m", "gateahead.runtime"]
