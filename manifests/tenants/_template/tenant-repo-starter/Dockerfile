# This is a normal Dockerfile. Build and test it locally exactly like any
# other Docker project you've ever written — nothing about Kubernetes
# changes how this file works.
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir flask
EXPOSE 8080
CMD ["python", "app.py"]
