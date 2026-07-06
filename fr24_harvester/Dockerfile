FROM python:3.12-slim
WORKDIR /app
COPY harvester.py .
RUN pip install --no-cache-dir requests
# Unbuffered → Logs erscheinen sofort (journalctl/docker logs)
ENV PYTHONUNBUFFERED=1
CMD ["python", "harvester.py"]
