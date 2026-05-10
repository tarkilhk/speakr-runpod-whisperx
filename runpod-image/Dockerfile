FROM learnedmachine/whisperx-asr-service:latest

ENV SERVE_MODE=simple

RUN apt-get update \
    && apt-get install -y --no-install-recommends supervisor \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --no-cache-dir \
        fastapi==0.115.6 \
        httpx==0.28.1 \
        uvicorn[standard]==0.34.0

RUN mkdir -p /opt/whisperx-asr \
    && cp -a /workspace/app /opt/whisperx-asr/app

WORKDIR /opt/whisperx-wrapper
COPY wrapper.py /opt/whisperx-wrapper/wrapper.py
COPY supervisord.conf /etc/supervisor/conf.d/whisperx-runpod.conf

EXPOSE 9000

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/whisperx-runpod.conf"]
