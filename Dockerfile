# Stage 1: Build requirements
FROM python:3.11-alpine as requirements

WORKDIR /app

COPY requirements.txt . 
RUN pip install --no-cache-dir -r requirements.txt


# Stage 2: Final image
FROM python:3.11-alpine as app

# Install necessary OS packages
RUN apk add --no-cache dcron libc6-compat poppler-utils bash

WORKDIR /app

# Copy requirements and install without dependencies
COPY --from=requirements /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=requirements /usr/local/bin /usr/local/bin

COPY . .

# Make sure the script is executable
RUN chmod +x ./initial_script.sh

# Start cron in background and run the script
CMD ["sh", "-c", "crond -b && ./initial_script.sh"]
