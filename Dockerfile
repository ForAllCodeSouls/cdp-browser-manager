FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt update && apt install -y --no-install-recommends \
    python3 python3-pip \
    chromium-browser \
    xvfb x11vnc novnc websockify \
    fonts-noto-cjk \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app/ /app/

EXPOSE 8000
EXPOSE 9222-9322
EXPOSE 7901-8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
