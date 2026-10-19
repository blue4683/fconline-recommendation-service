FROM python:3.11.15-slim-bookworm@sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir pip==26.2.1 \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip check
COPY FRS ./FRS
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "FRS.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
