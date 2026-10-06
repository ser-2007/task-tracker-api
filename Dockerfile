FROM python:3.12-slim

# Pick up OS-level security patches (e.g. libpcre2 CVE-2026-103111) that
# land after the base image was built. Found by the CI pipeline's Trivy
# scan actually blocking a real fixable HIGH CVE — see README.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV DB_PATH=/data/tasks.db
RUN mkdir -p /data

EXPOSE 8080

# gunicorn, not the Flask dev server — this runs as a real container image
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "app:app"]