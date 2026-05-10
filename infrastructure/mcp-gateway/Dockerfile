# Minimal image: Python + kubectl + awslabs eks-mcp-server + FastAPI shim.
# kubectl is needed because eks-mcp-server shells out to it for some tools.
FROM public.ecr.aws/docker/library/python:3.12-slim

ARG KUBECTL_VERSION=v1.30.0
ARG TARGETARCH=arm64

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -sSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
 && chmod +x /usr/local/bin/kubectl \
 && apt-get purge -y curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/

ENV PORT=8080 \
    LOG_LEVEL=INFO \
    AWS_REGION=us-east-1

EXPOSE 8080

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
