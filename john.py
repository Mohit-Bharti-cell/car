from flask import Flask, request, jsonify
import pyodbc
import cloudinary
import cloudinary.uploader
import boto3
from dotenv import load_dotenv
from flask_cors import CORS
import os
import logging
from uuid import uuid4
import requests

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Flask app initialization
app = Flask(__name__)
CORS(app)  # Enable CORS for all domains

# Cloudinary configuration
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)

# AWS S3 Configuration
s3 = boto3.client(
    's3',
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
    region_name=os.getenv('AWS_REGION')
)

S3_BUCKET = os.getenv('S3_BUCKET_NAME')

# Azure SQL Database configuration
db_config = {
    'server': os.getenv('AZURE_SQL_SERVER'),  # Azure SQL Server name
    'database': os.getenv('AZURE_SQL_DATABASE'),  # Database name (car_rent)
    'user': os.getenv('AZURE_SQL_USER'),  # Database username
    'password': os.getenv('AZURE_SQL_PASSWORD'),  # Database password
}

# Table name
table_name = 'cars'  # Your table name in the database

# Utility function to check if a URL is accessible
def is_url_accessible(url):
    try:
        response = requests.head(url, timeout=5)
        return response.status_code == 200
    except requests.RequestException as e:
        logger.error(f"URL access error: {e}")
        return False

# Function to upload an image from S3 to Cloudinary
def upload_image_to_cloudinary_from_s3(s3_url):
    try:
        response = cloudinary.uploader.upload(s3_url)
        return response.get("secure_url")
    except Exception as e:
        logger.error(f"Error uploading image to Cloudinary: {e}")
        return None

# Function to insert car details into the database
def insert_car_details(car_name, segment_id, segment_name, model_type, year, engine_type, fuel_type, price, image_urls, cursor):
    try:
        cursor.execute(f"""
            SELECT 1 
            FROM {table_name}
            WHERE segment_id = ? AND segment_name = ? AND model_type = ?
        """, (segment_id, segment_name, model_type))
        if cursor.fetchone():
            return f"Model '{model_type}' already exists under segment ID '{segment_id}' and segment name '{segment_name}'."

        cursor.execute(f"""
            INSERT INTO {table_name} (car_name, segment_id, segment_name, model_type, year, engine_type, fuel_type, price,
                                     image_data, front_view, back_view, left_side_view, right_side_view)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            car_name, segment_id, segment_name, model_type, year, engine_type, fuel_type, price,
            image_urls.get("image_data"), image_urls.get("front_view"),
            image_urls.get("back_view"), image_urls.get("left_side_view"),
            image_urls.get("right_side_view")
        ))
        cursor.commit()
        return "Car details inserted successfully."
    except pyodbc.Error as e:
        logger.error(f"Database error: {e}")
        return None

@app.route('/upload_car', methods=['POST'])
def upload_car():
    data = request.json

    # Validate required fields
    required_fields = ['car_name', 'segment_id', 'segment_name', 'model_type', 'year', 'engine_type', 'fuel_type', 'price', 'image_paths']
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    car_name = data['car_name']
    segment_id = data['segment_id']
    segment_name = data['segment_name']
    model_type = data['model_type']
    year = data['year']
    engine_type = data['engine_type']
    fuel_type = data['fuel_type']
    price = data['price']
    image_paths = data['image_paths']  # Dictionary of S3 image file paths

    image_urls = {}
    errors = {}

    # Upload images from S3 to Cloudinary
    for column, s3_url in image_paths.items():
        if not is_url_accessible(s3_url):
            errors[column] = f"S3 URL for {column} is not accessible"
            continue

        image_url = upload_image_to_cloudinary_from_s3(s3_url)
        if image_url:
            image_urls[column] = image_url
        else:
            errors[column] = f"Failed to upload image for {column}"

    if errors:
        return jsonify({"error": "Some images failed to upload", "details": errors}), 207  # Multi-Status

    try:
        # Updated Azure SQL Database connection
        conn = pyodbc.connect(
            f"Driver={{ODBC Driver 18 for SQL Server}};"
            f"Server={db_config['server']},1433;"
            f"Database={db_config['database']};"
            f"UID={db_config['user']};"
            f"PWD={db_config['password']};"
            f"Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
        )
        cursor = conn.cursor()

        result = insert_car_details(car_name, segment_id, segment_name, model_type, year, engine_type, fuel_type, price, image_urls, cursor)

        if not result:
            return jsonify({"error": "Failed to insert car details"}), 500

        if "already exists" in result:
            return jsonify({"message": result}), 200

        return jsonify({"message": result}), 201

    except pyodbc.Error as e:
        logger.error(f"Database error: {e}")
        return jsonify({"error": "Database error occurred"}), 500
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

# Main entry point
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
