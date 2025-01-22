import os
from flask import Flask, request, jsonify
import cloudinary
import cloudinary.uploader
import pyodbc
import logging
import cv2
import numpy as np
import urllib.request
from dotenv import load_dotenv
from contextlib import closing

# Load environment variables from .env file
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# Cloudinary configuration from environment variables
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)

# Database configuration from environment variables
db_config = {
    'server': os.getenv('AZURE_SQL_SERVER'),
    'database': os.getenv('AZURE_SQL_DATABASE'),
    'user': os.getenv('AZURE_SQL_USER'),
    'password': os.getenv('AZURE_SQL_PASSWORD'),
}

# Logger configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Helper function to retrieve image URL from database
def retrieve_image_url_from_db(segment_id, model_type, column, db_config):
    try:
        conn_str = (
            f"Driver={{ODBC Driver 18 for SQL Server}};"
            f"Server={db_config['server']},1433;"
            f"Database={db_config['database']};"
            f"UID={db_config['user']};"
            f"PWD={db_config['password']};"
        )
        with closing(pyodbc.connect(conn_str)) as conn:
            with conn.cursor() as cursor:
                query = f"""
                    SELECT scooter_id, segment_id, segment_name, model_type, {column}
                    FROM scooter_ev
                    WHERE model_type = ? AND segment_id = ?
                """
                cursor.execute(query, (model_type, segment_id))
                results = cursor.fetchall()

                return [
                    {
                        'scooter_id': row[0],
                        'segment_id': row[1],
                        'segment_name': row[2],
                        'model_type': row[3],
                        'image_url': row[4]
                    }
                    for row in results
                ]
    except pyodbc.Error as e:
        logging.error(f"Database error: {e}")
        return []

# Helper function to upload images to Cloudinary
def upload_image_to_cloudinary(image_url):
    try:
        response = cloudinary.uploader.upload(image_url)
        return response['secure_url']
    except Exception as e:
        logging.error(f"Error uploading image to Cloudinary: {e}")
        return None

# Helper function to detect scratches or differences between new and existing images
def detect_scratches_or_differences(new_image_url, existing_image_url):
    try:
        # Load new image from URL
        resp_new = urllib.request.urlopen(new_image_url)
        new_image_data = np.asarray(bytearray(resp_new.read()), dtype="uint8")
        new_image = cv2.imdecode(new_image_data, cv2.IMREAD_COLOR)

        # Load existing image from URL
        resp_existing = urllib.request.urlopen(existing_image_url)
        existing_image_data = np.asarray(bytearray(resp_existing.read()), dtype="uint8")
        existing_image = cv2.imdecode(existing_image_data, cv2.IMREAD_COLOR)

        if new_image is None or existing_image is None:
            logging.error("Failed to load images.")
            return True  # Default to update if images can't be compared

        # Resize images for comparison
        new_image_resized = cv2.resize(new_image, (500, 500))
        existing_image_resized = cv2.resize(existing_image, (500, 500))

        # Convert to grayscale
        new_gray = cv2.cvtColor(new_image_resized, cv2.COLOR_BGR2GRAY)
        existing_gray = cv2.cvtColor(existing_image_resized, cv2.COLOR_BGR2GRAY)

        # Compute absolute difference
        diff_image = cv2.absdiff(new_gray, existing_gray)

        # Blur and detect edges
        blurred_diff = cv2.GaussianBlur(diff_image, (5, 5), 0)
        edges = cv2.Canny(blurred_diff, 20, 100)

        # Find contours
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # Check for significant differences
        for contour in contours:
            if cv2.contourArea(contour) > 10:
                return True

        return False
    except Exception as e:
        logging.error(f"Error detecting scratches: {e}")
        return False

# Endpoint for processing images
@app.route('/process-images', methods=['POST'])
def process_images():
    try:
        data = request.get_json()
        model_type = data['model_type']
        segment_id = data['segment_id']
        image_paths = data['image_paths']  # Using `image_paths`

        if not all([model_type, segment_id, image_paths]):
            return jsonify({"error": "Missing required parameters"}), 400

        results = []

        for column, new_image_url in image_paths.items():
            scooters = retrieve_image_url_from_db(segment_id, model_type, column, db_config)

            for scooter in scooters:
                existing_image_url = scooter['image_url']
                response = {"column": column}

                if existing_image_url and detect_scratches_or_differences(new_image_url, existing_image_url):
                    new_image_url_cloudinary = upload_image_to_cloudinary(new_image_url)
                    if new_image_url_cloudinary:
                        try:
                            conn_str = (
                                f"Driver={{ODBC Driver 18 for SQL Server}};"
                                f"Server={db_config['server']},1433;"
                                f"Database={db_config['database']};"
                                f"UID={db_config['user']};"
                                f"PWD={db_config['password']};"
                            )
                            with closing(pyodbc.connect(conn_str)) as conn:
                                with conn.cursor() as cursor:
                                    cursor.execute(f"""
                                        UPDATE scooter_ev
                                        SET {column} = ?
                                        WHERE segment_id = ? AND model_type = ?
                                    """, (new_image_url_cloudinary, segment_id, model_type))
                                    conn.commit()

                                response["new_image_url"] = new_image_url_cloudinary
                                response["status"] = "Scratches detected, image updated"
                        except pyodbc.Error as e:
                            logging.error(f"Database error: {e}")
                            response["status"] = f"Database error: {str(e)}"
                else:
                    response["status"] = "No scratches detected, image retained"

                results.append(response)

        return jsonify(results), 200

    except Exception as e:
        logging.error(f"Error in process_images endpoint: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
