FROM python:3.13-alpine

RUN addgroup -g 20 dialout || true
RUN addgroup -S sensors && adduser -D -u 1000 -G sensors sensors
RUN addgroup sensors dialout

RUN pip install --no-cache-dir pyserial

WORKDIR /app

COPY server.py .

RUN mkdir /data && chown sensors:sensors /data

EXPOSE 8000

USER sensors
ENV PYTHONUNBUFFERED=1
CMD ["python", "server.py"]
