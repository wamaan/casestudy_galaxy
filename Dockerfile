FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the port on which the app runs
EXPOSE 8000

# Define environment variable (optional)
ENV FLASK_APP=app.py

# Run the application
CMD ["python", "app.py"]
