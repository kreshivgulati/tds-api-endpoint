FROM python:3.10-slim

# Install system level dependencies for Playwright
RUN apt-get update && apt-get install -y wget gnupg curl && \
    apt-get install -y fonts-ipafont-gothic libnss3 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libasound2 libxshmfence1 \
    libx11-xcb1 libxext6 libxfixes3 && \
    apt-get clean

# Install Python dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Install Playwright browsers
RUN playwright install --with-deps chromium

# Copy app code
COPY . .

# Expose port
EXPOSE 8000

# Start server
CMD ["uvicorn", "run:app", "--host", "0.0.0.0", "--port", "8000"]
