from flask import Flask, request, jsonify
import cloudinary
import os
import logging
from cloudinary.uploader import upload
from dotenv import load_dotenv
from flask_cors import CORS
import pyodbc

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

# Azure SQL Database configuration
db_config = {
    'server': os.getenv('AZURE_SQL_SERVER'),
    'database': os.getenv('AZURE_SQL_DATABASE'),
    'user': os.getenv('AZURE_SQL_USER'),
    'password': os.getenv('AZURE_SQL_PASSWORD'),
}

# Table name
table_name = 'cars'

# Function to upload an image to Cloudinary
def upload_image_to_cloudinary(file):
    try:
        # Upload to Cloudinary
        response = cloudinary.uploader.upload(file)
        logger.info(f"Image uploaded to Cloudinary: {response['secure_url']}")
        return response['secure_url']
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
    data = request.form  # Get form-data (including files)

    # Validate required fields
    required_fields = ['car_name', 'segment_id', 'segment_name', 'model_type', 'year', 'engine_type', 'fuel_type', 'price']
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    # Get car details from the form-data
    car_name = data['car_name']
    segment_id = data['segment_id']
    segment_name = data['segment_name']
    model_type = data['model_type']
    year = data['year']
    engine_type = data['engine_type']
    fuel_type = data['fuel_type']
    price = data['price']

    # Process images
    image_urls = {}
    errors = {}

    image_fields = ['image_data', 'front_view', 'back_view', 'left_side_view', 'right_side_view']
    for field in image_fields:
        if field in request.files:
            image_file = request.files[field]
            logger.info(f"Processing {field}: {image_file.filename}")
            # Upload the image to Cloudinary
            image_url = upload_image_to_cloudinary(image_file)
            if image_url:
                image_urls[field] = image_url
            else:
                errors[field] = f"Failed to upload image for {field}"
        else:
            errors[field] = f"File not found for {field}"

    # If there are errors, return them
    if errors:
        return jsonify({"error": "Some images failed to upload", "details": errors}), 207  # Multi-Status

    try:
        # Connect to Azure SQL Database
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
