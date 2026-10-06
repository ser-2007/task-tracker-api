FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV DB_PATH=/data/tasks.db
RUN mkdir -p /data

EXPOSE 8080

# gunicorn, not the Flask dev server — this runs as a real container image
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "app:app"]
