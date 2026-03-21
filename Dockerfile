# Use official Python 3.11 slim image
FROM python:3.11-slim

# Install Chromium + dependencies for Selenium
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    wget \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Tell Selenium to use Chromium
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

# Set working directory
WORKDIR /app

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Create directories for reports, logs, and resume mount point
RUN mkdir -p reports logs /resume

# Expose the web UI port
EXPOSE 5050

# Run the server
CMD ["python3", "build_report.py", "--serve"]
