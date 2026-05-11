FROM learnedmachine/whisperx-asr-service:latest

ENV SERVE_MODE=simple
ENV HOME=/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends supervisor \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --no-cache-dir \
        fastapi==0.115.6 \
        httpx==0.28.1 \
        uvicorn[standard]==0.34.0

RUN mkdir -p /opt/whisperx-asr \
    && cp -a /workspace/app /opt/whisperx-asr/app \
    && mkdir -p /var/log/whisperx-pod \
    && chown 1000:1000 /var/log/whisperx-pod

WORKDIR /opt/whisperx-wrapper
COPY runpod-image/wrapper.py /opt/whisperx-wrapper/wrapper.py
COPY runpod-image/tee_process.py /opt/whisperx-wrapper/tee_process.py
COPY speakr_common /opt/whisperx-wrapper/speakr_common
COPY runpod-image/supervisord.conf /etc/supervisor/conf.d/whisperx-runpod.conf

EXPOSE 9000

USER 1000:1000

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/whisperx-runpod.conf"]
