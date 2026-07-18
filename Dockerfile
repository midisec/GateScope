FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV HOST=0.0.0.0 PORT=8777 NO_BROWSER=1 PYTHONUNBUFFERED=1
VOLUME ["/app/data"]
EXPOSE 8777
CMD ["python", "app.py"]
