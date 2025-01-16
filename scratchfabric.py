import logging
import os
import cloudinary
import cloudinary.uploader
import pyodbc
import cv2
import numpy as np
import urllib.request
from flask import Flask, request, jsonify
from flask_restx import Api, Resource, fields
from flask_cors import CORS
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Cloudinary Configuration
cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET')
)

# Flask app and API initialization
app = Flask(__name__)
api = Api(app)

# Enable CORS (Cross-Origin Resource Sharing)
CORS(app)

# API Models for Swagger Docs
image_upload_model = api.model('ImageUpload', {
    'segment_id': fields.Integer(required=True, description='Segment ID of the car'),
    'model_type': fields.String(required=True, description='Model type of the car'),
    'image_paths': fields.Raw(required=True, description='A dictionary of image paths with column names as keys')
})

# Database Configuration from .env file
db_config = {
    'server': os.getenv('FABRIC_SERVER'),
    'database': os.getenv('FABRIC_DATABASE'),
    'user': os.getenv('FABRIC_USER'),
    'password': os.getenv('FABRIC_PASSWORD'),
    'authentication': 'ActiveDirectoryPassword',  # Use password-based authentication
}

def retrieve_image_url_from_db(segment_id, model_type, column, db_config):
    """Retrieve the image URL for the specified segment_id and model_type from the database."""
    try:
        conn_str = (
            f"Driver={{ODBC Driver 18 for SQL Server}};"
            f"Server={db_config['server']},1433;"
            f"Database={db_config['database']};"
            f"UID={db_config['user']};"
            f"PWD={db_config['password']};"
            f"Authentication={db_config['authentication']};"
        )
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()

        query = f"""
            SELECT car_id, segment_id, segment_name, model_type, {column}
            FROM car
            WHERE model_type = ? AND segment_id = ?
        """
        
        cursor.execute(query, (model_type, segment_id))
        results = cursor.fetchall()

        if results:
            cars = []
            for row in results:
                cars.append({
                    'car_id': row[0],
                    'segment_id': row[1],
                    'segment_name': row[2],
                    'model_type': row[3],
                    'image_url': row[4]
                })
            logging.info(f"Successfully retrieved image URLs for segment_id '{segment_id}' and model_type '{model_type}'.")
            return cars
        else:
            logging.warning(f"No cars found for model_type '{model_type}' and segment_id '{segment_id}'.")
            return []
    except pyodbc.Error as e:
        logging.error(f"Error retrieving image URLs for segment_id '{segment_id}' and model_type '{model_type}': {e}")
        return []
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def upload_image_to_cloudinary(image_path):
    """Upload image to Cloudinary and return the secure URL."""
    try:
        response = cloudinary.uploader.upload(image_path)
        return response['secure_url']
    except Exception as e:
        logging.error(f"Error uploading image to Cloudinary: {e}")
        return None

def detect_scratches_or_differences(new_image_path, existing_image_url):
    """Detect scratches or differences between the new image and the existing image."""
    try:
        # Load new image
        new_image = cv2.imread(new_image_path, cv2.IMREAD_GRAYSCALE)
        if new_image is None:
            logging.error("Failed to load the new image.")
            return False

        # Load existing image from the URL
        with urllib.request.urlopen(existing_image_url) as resp:
            existing_image_data = np.asarray(bytearray(resp.read()), dtype="uint8")
        existing_image = cv2.imdecode(existing_image_data, cv2.IMREAD_GRAYSCALE)
        if existing_image is None:
            logging.error("Failed to load the existing image from the URL.")
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

        # Save intermediate difference image
        cv2.imwrite("debug_diff_image.jpg", diff_image)

        # Blur and detect edges in the difference image
        blurred_diff = cv2.GaussianBlur(diff_image, (7, 7), 0)
        edges = cv2.Canny(blurred_diff, 50, 200)

        # Save blurred and edge-detected images for debugging
        cv2.imwrite("debug_blurred_diff.jpg", blurred_diff)
        cv2.imwrite("debug_edges.jpg", edges)

        # Morphological operations to close small gaps
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        morphed_edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
        cv2.imwrite("debug_morphed_edges.jpg", morphed_edges)

        # Find contours in the morphed edge image
        contours, _ = cv2.findContours(morphed_edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # Check if any contour area exceeds the threshold
        scratch_detected = False
        for contour in contours:
            area = cv2.contourArea(contour)
            if area > 30:  # Adjust the area threshold as needed
                logging.info(f"Scratch detected with area: {area}")
                scratch_detected = True

        # Log the result
        if scratch_detected:
            logging.info("Scratches or differences detected.")
        else:
            logging.info("No significant scratches or differences detected.")

        return scratch_detected

    except Exception as e:
        logging.error(f"Error detecting scratches or differences in images: {e}")
        return False


def update_images_for_segment(segment_id, model_type, image_paths, db_config):
    """Update Cloudinary image URLs for all cars matching the model_type and segment_id in the database."""
    result = []

    for column, new_image_path in image_paths.items():
        cars = retrieve_image_url_from_db(segment_id, model_type, column, db_config)

        for car in cars:
            existing_image_url = car['image_url']

            if existing_image_url:
                issues_detected = detect_scratches_or_differences(new_image_path, existing_image_url)
                if issues_detected:
                    new_image_url = upload_image_to_cloudinary(new_image_path)
                    if new_image_url:
                        try:
                            conn_str = (
                                f"Driver={{ODBC Driver 18 for SQL Server}};"
                                f"Server={db_config['server']};"
                                f"Database={db_config['database']};"
                                f"UID={db_config['user']};"
                                f"PWD={db_config['password']};"
                                f"Authentication={db_config['authentication']};"
                            )
                            conn = pyodbc.connect(conn_str)
                            cursor = conn.cursor()
                            cursor.execute(f"UPDATE car SET {column} = ? WHERE segment_id = ? AND model_type = ?", (new_image_url, segment_id, model_type))
                            conn.commit()
                            cursor.close()
                            conn.close()
                            result.append({'column': column, 'status': 'scratch detected and image updated'})
                        except Exception as e:
                            result.append({'column': column, 'status': f'Error: {e}'})
                else:
                    result.append({'column': column, 'status': 'No change'})
    return result


@api.route('/upload-images')
class UploadImages(Resource):
    def post(self):
        data = request.json
        segment_id = data.get('segment_id')
        model_type = data.get('model_type')
        image_paths = data.get('image_paths')

        if not segment_id or not model_type or not image_paths:
            return {'status': 'error', 'message': 'Invalid input parameters'}, 400

        result = update_images_for_segment(segment_id, model_type, image_paths, db_config)
        return jsonify(result)


if __name__ == '__main__':
    app.run(debug=True)
