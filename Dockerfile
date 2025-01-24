# Use Debian 11 as the base image
FROM debian:11

# Set environment variable to accept EULA for Microsoft ODBC Driver
ENV ACCEPT_EULA=Y

# Install prerequisites and Microsoft ODBC Driver 18 for SQL Server
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    unixodbc \
    unixodbc-dev && \
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/11/prod bullseye main" > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && apt-get install -y msodbcsql18 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
