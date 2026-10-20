FROM python:3.11-slim

# Pinned to the versions the deployed runner is known to work with, so a
# rebuild on another day or another machine produces the same runner.
RUN pip install kubernetes==36.0.3 paramiko==5.0.0

RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh

WORKDIR /scripts
COPY ansible-runner.py .

CMD ["python3", "-u", "ansible-runner.py"]
