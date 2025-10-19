FROM python:3.11-slim

WORKDIR /app
COPY . /app

RUN pip install uv
RUN apt-get update && apt-get install -y make
RUN make install

EXPOSE 8001
ENV ACP2_AUTH_TOKEN="your-secret-token"

CMD ["uvicorn", "src.acp2_proxy.main:create_app", "--host", "0.0.0.0", "--port", "8001"]