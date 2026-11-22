FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run as an ordinary user, not root.
RUN useradd --create-home --uid 10001 casabus
COPY *.py index.html ./
USER casabus

EXPOSE 5000

CMD ["python3", "app.py"]
