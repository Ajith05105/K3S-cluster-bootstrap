FROM python:3.11-slim

RUN pip install kubernetes paramiko

RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh

WORKDIR /scripts
COPY ansible-runner.py .

CMD ["python3", "-u", "ansible-runner.py"]
