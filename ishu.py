import logging
import os
import cloudinary
import cloudinary.uploader
import pyodbc
from flask import Flask, request, jsonify
from flask_restx import Api, Resource, fields
from flask_cors import CORS
from dotenv import load_dotenv
import urllib
import numpy as np
import cv2

# Load environment variables from .env file
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Cloudinary Configuration
cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET')
)

# Database Configuration from .env file
db_config = {
    'server': os.getenv('AZURE_SQL_SERVER'),
    'database': os.getenv('AZURE_SQL_DATABASE'),
    'user': os.getenv('AZURE_SQL_USER'),
    'password': os.getenv('AZURE_SQL_PASSWORD'),
}

# Flask app and API initialization
app = Flask(__name__)
api = Api(app)
CORS(app)

# API Models for Swagger Docs
image_upload_model = api.model('ImageUpload', {
    'segment_id': fields.Integer(required=True, description='Segment ID of the car'),
    'model_type': fields.String(required=True, description='Model type of the car'),
    # Removed 'image_paths' since files are being sent as multipart data
})

def upload_image_to_cloudinary(file):
    try:
        response = cloudinary.uploader.upload(file)
        logger.info(f"Image uploaded to Cloudinary: {response['secure_url']}")
        return response['secure_url']
    except Exception as e:
        logger.error(f"Error uploading image to Cloudinary: {e}")
        return None

def detect_scratches_or_differences(new_image_url, existing_image_url):
    """Detect scratches or differences between the new image and the existing image."""
    try:
        # Load new image from Cloudinary URL
        with urllib.request.urlopen(new_image_url) as resp:
            new_image_data = np.asarray(bytearray(resp.read()), dtype="uint8")
        new_image = cv2.imdecode(new_image_data, cv2.IMREAD_GRAYSCALE)
        if new_image is None:
            logging.error("Failed to load the new image from Cloudinary.")
            return False

        # Load existing image from the URL in database
        with urllib.request.urlopen(existing_image_url) as resp:
            existing_image_data = np.asarray(bytearray(resp.read()), dtype="uint8")
        existing_image = cv2.imdecode(existing_image_data, cv2.IMREAD_GRAYSCALE)
        if existing_image is None:
            logging.error("Failed to load the existing image from the database.")
            return True

        # Resize images to the same dimensions
        height, width = 500, 500
        new_image_resized = cv2.resize(new_image, (width, height))
        existing_image_resized = cv2.resize(existing_image, (width, height))

        # Contrast enhancement using CLAHE
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        new_image_enhanced = clahe.apply(new_image_resized)
        existing_image_enhanced = clahe.apply(existing_image_resized)

        # Calculate the absolute difference between the images
        diff_image = cv2.absdiff(new_image_enhanced, existing_image_enhanced)

        # Blur and detect edges in the difference image
        blurred_diff = cv2.GaussianBlur(diff_image, (7, 7), 0)
        edges = cv2.Canny(blurred_diff, 50, 200)

        # Morphological operations to close small gaps
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        morphed_edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

        # Find contours in the morphed edge image
        contours, _ = cv2.findContours(morphed_edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # Check if any contour area exceeds the threshold
        scratch_detected = any(cv2.contourArea(contour) > 10 for contour in contours)  # Lower threshold to detect smaller scratches

        if scratch_detected:
            logging.info("Scratch detected!")
        else:
            logging.info("No scratch detected.")

        return scratch_detected

    except Exception as e:
        logging.error(f"Error detecting scratches or differences in images: {e}")
        return False

def retrieve_image_url_from_db(segment_id, model_type, column, db_config):
    """
    Retrieves an image URL from the database for the specified segment ID, model type, and column.
    """
    try:
        conn_str = (
            f"Driver={{ODBC Driver 17 for SQL Server}};"
            f"Server={db_config['server']};"
            f"Database={db_config['database']};"
            f"UID={db_config['user']};"
            f"PWD={db_config['password']};"
            f"Encrypt=yes;TrustServerCertificate=no;Connection Timeout=300;"
        )
        conn = pyodbc.connect(conn_str)

        cursor = conn.cursor()
        query = f"SELECT {column} AS image_url FROM cars WHERE segment_id = ? AND model_type = ?"
        cursor.execute(query, (segment_id, model_type))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return [{'image_url': row.image_url} for row in rows]
    except Exception as e:
        logger.error(f"Error retrieving image URL from database: {e}")
        return []

def update_images_for_segment(segment_id, model_type, files, db_config):
    """
    Updates images for a specific car segment by uploading to Cloudinary and updating the database.
    """
    result = []
    for column, file in files.items():
        # Upload image to Cloudinary
        cloudinary_url = upload_image_to_cloudinary(file)
        if not cloudinary_url:
            result.append({'column': column, 'status': 'Failed to upload to Cloudinary'})
            continue

        # Retrieve existing image URLs from the database
        cars = retrieve_image_url_from_db(segment_id, model_type, column, db_config)
        for car in cars:
            existing_image_url = car['image_url']
            scratch_detected = detect_scratches_or_differences(cloudinary_url, existing_image_url)
            
            # If scratch detected, update the database
            if scratch_detected:
                try:
                    # Update the database with the new Cloudinary URL
                    conn_str = (
                        f"Driver={{ODBC Driver 18 for SQL Server}};"
                        f"Server={db_config['server']};"
                        f"Database={db_config['database']};"
                        f"UID={db_config['user']};"
                        f"PWD={db_config['password']};"
                    )
                    conn = pyodbc.connect(conn_str)
                    cursor = conn.cursor()
                    cursor.execute(
                        f"UPDATE cars SET {column} = ? WHERE segment_id = ? AND model_type = ?",
                        (cloudinary_url, segment_id, model_type)
                    )
                    conn.commit()
                    cursor.close()
                    conn.close()
                    result.append({'column': column, 'status': 'Updated with scratches detected'})
                except Exception as e:
                    result.append({'column': column, 'status': f'Error updating DB: {e}'})
            else:
                result.append({'column': column, 'status': 'No scratch detected, no update made'})
                
    return result

@api.route('/upload-images-with-keys')
class UploadImagesWithKeys(Resource):
    @api.expect(image_upload_model)  # Model validation for API input
    def post(self):
        try:
            # Get segment_id and model_type
            segment_id = request.form.get('segment_id')
            model_type = request.form.get('model_type')

            # Get files from the form
            image_files = request.files.to_dict()

            # Validate input parameters
            if not segment_id or not model_type or not image_files:
                return {'status': 'error', 'message': 'Invalid input parameters'}, 400

            logger.info(f"Received segment_id: {segment_id}, model_type: {model_type}")

            # Update the database with Cloudinary URLs
            result = update_images_for_segment(segment_id, model_type, image_files, db_config)

            return {'status': 'success', 'data': result}, 200

        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return {'status': 'error', 'message': 'Server error, please try again later'}, 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
