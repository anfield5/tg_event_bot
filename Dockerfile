# -------------------------------------------
# Dockerfile for Fly.io deployment
# -------------------------------------------
# This Dockerfile is required by Fly.io to build and run your bot inside a container.
# Fly.io runs your application in a container, so we need to specify the environment,
# install dependencies, copy your code, and define the startup command.
# -------------------------------------------

# Use the official slim Python 3.11 image as a base
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the dependencies file and install all dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project into the container
COPY . .

# Define the command to run your bot
# Replace 'bot.py' with your main Python file if different
CMD ["python", "bot.py"]
