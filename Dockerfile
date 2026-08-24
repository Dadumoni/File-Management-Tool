FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# System updates aur FFmpeg install karein
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements copy aur install karein
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download matching chromium binaries for the installed python package
RUN playwright install chromium

# Application code copy karein
COPY . .

# Koyeb Health Check Port
EXPOSE 8080

# Script start karein
CMD ["python", "main.py"]
