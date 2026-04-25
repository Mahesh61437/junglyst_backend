# Use an official Python runtime as a parent image
FROM python:3.14-slim

# Set environment variables for high-performance Python
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

# Set the working directory in the container
WORKDIR /app

# Install system dependencies & debugging tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    telnet \
    net-tools \
    iputils-ping \
    vim-tiny \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the project code into the container
COPY . .

# Create directory for logs
RUN mkdir -p logs

# Collect static files
RUN python manage.py collectstatic --noinput

# Expose the port the app runs on
EXPOSE 8080

# Use gunicorn as the production server
CMD gunicorn junglyst_backend.wsgi:application --bind 0.0.0.0:$PORT --workers 3 --threads 2 --timeout 60
