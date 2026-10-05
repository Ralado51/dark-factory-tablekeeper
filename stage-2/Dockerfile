FROM python:3.12-slim
WORKDIR /app
COPY app.py index.html app.js style.css /app/
ENV PORT=8080 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
USER 65534:65534
CMD ["python", "/app/app.py"]
