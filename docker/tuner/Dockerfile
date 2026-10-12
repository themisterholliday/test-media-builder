# The tuner needs Python and ffmpeg and nothing else. Installing ffmpeg at
# container start instead would re-download it on every `up` and fail offline.
FROM python:3.12-alpine

RUN apk add --no-cache ffmpeg

COPY tuner.py /app/tuner.py

# Unbuffered, so the request log reaches `docker logs` as it happens rather than
# when the buffer fills. Diagnosing a tuner is mostly reading that log.
ENV PYTHONUNBUFFERED=1

EXPOSE 80
EXPOSE 65001/udp

CMD ["python", "/app/tuner.py"]
