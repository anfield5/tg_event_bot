# -------------------------------------------
# Dockerfile for Fly.io deployment
# -------------------------------------------
# This Dockerfile is required by Fly.io to build and run your bot inside a container.
# Fly.io runs your application in a container, so we need to specify the environment,
# install dependencies, copy your code, and define the startup command.
# -------------------------------------------

# Use the official slim Python 3.11 image as a base
FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies (needed for some Python libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the dependencies file and install all dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project into the container
COPY . .

# Define the command to run your bot
# Replace 'bot.py' with your main Python file if different
CMD ["python", "bot.py"]
