FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY predict_order_notifier.py .

CMD ["python", "predict_order_notifier.py"]
