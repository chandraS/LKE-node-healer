FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY node_healer.py .

RUN useradd -m -u 1000 healer
USER healer

CMD ["python", "-u", "node_healer.py"]
