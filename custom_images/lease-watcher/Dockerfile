FROM python:3.11-slim

RUN pip install kubernetes

WORKDIR /scripts
COPY watcher.py .

CMD ["python3", "watcher.py"]
