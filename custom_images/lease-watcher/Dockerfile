FROM python:3.11-slim

RUN pip install kubernetes

WORKDIR /scripts
COPY watcher.py .

# -u keeps stdout unbuffered, matching ansible-runner. Without it Python
# block-buffers when writing to a pipe, so log lines only surface once a few
# KB accumulate — which made this container look wedged during an incident
# when it was actually running normally.
CMD ["python3", "-u", "watcher.py"]
