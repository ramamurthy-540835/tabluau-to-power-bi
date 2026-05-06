FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p input_workbooks pbi_output

ENV PORT=8080
EXPOSE 8080

CMD uvicorn ui.web.main:app --host 0.0.0.0 --port $PORT
