import os
from flask import Flask, request, jsonify
import cloudinary
import cloudinary.uploader
import pyodbc
import logging
import cv2
import numpy as np
import urllib.request
from dotenv import load_dotenv  # Import load_dotenv
from contextlib import closing  # Add this import

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
    'server': os.getenv('FABRIC_SERVER'),
    'database': os.getenv('FABRIC_DATABASE'),
    'user': os.getenv('FABRIC_USER'),
    'password': os.getenv('FABRIC_PASSWORD'),
    'authentication': 'ActiveDirectoryPassword',
}

# Logger configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Helper function to retrieve image URL from database
def retrieve_image_url_from_db(segment_id, model_type, column, db_config):
    try:
        conn_str = (
            f"Driver={{ODBC Driver 18 for SQL Server}};"
            f"Server={db_config['server']},1433;"  # Ensuring port is included
            f"Database={db_config['database']};"
            f"UID={db_config['user']};"
            f"PWD={db_config['password']};"
            f"Authentication={db_config['authentication']};"
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
def upload_image_to_cloudinary(image_path):
    try:
        response = cloudinary.uploader.upload(image_path)
        return response['secure_url']
    except Exception as e:
        logging.error(f"Error uploading image to Cloudinary: {e}")
        return None

# Helper function to detect scratches or differences between new and existing images
def detect_scratches_or_differences(new_image_path, existing_image_url):
    try:
        new_image = cv2.imread(new_image_path)
        if new_image is None:
            logging.error("Failed to load the new image.")
            return False

        resp = urllib.request.urlopen(existing_image_url)
        existing_image_data = np.asarray(bytearray(resp.read()), dtype="uint8")
        existing_image = cv2.imdecode(existing_image_data, cv2.IMREAD_COLOR)
        if existing_image is None:
            logging.error("Failed to load the existing image from Cloudinary.")
            return True

        new_image_resized = cv2.resize(new_image, (500, 500))
        existing_image_resized = cv2.resize(existing_image, (500, 500))

        new_gray = cv2.cvtColor(new_image_resized, cv2.COLOR_BGR2GRAY)
        existing_gray = cv2.cvtColor(existing_image_resized, cv2.COLOR_BGR2GRAY)

        diff_image = cv2.absdiff(new_gray, existing_gray)
        diff_image = cv2.normalize(diff_image, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX)

        blurred_diff = cv2.GaussianBlur(diff_image, (5, 5), 0)
        edges = cv2.Canny(blurred_diff, 20, 100)

        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for contour in contours:
            if cv2.contourArea(contour) > 10:
                return True

        return False
    except Exception as e:
        logging.error(f"Error detecting scratches: {e}")
        return False

# Endpoint for uploading images
@app.route('/upload-images', methods=['POST'])
def upload_images():
    try:
        data = request.get_json()
        model_type = data['model_type']
        segment_id = data['segment_id']
        image_paths = data['image_paths']

        if not all([model_type, segment_id, image_paths]):
            return jsonify({"error": "Missing required parameters"}), 400

        results = []

        for column, new_image_path in image_paths.items():
            scooters = retrieve_image_url_from_db(segment_id, model_type, column, db_config)

            for scooter in scooters:
                existing_image_url = scooter['image_url']
                response = {"column": column}

                if not os.path.exists(new_image_path):
                    response["status"] = f"File not found: {new_image_path}"
                    results.append(response)
                    continue

                if existing_image_url and detect_scratches_or_differences(new_image_path, existing_image_url):
                    new_image_url = upload_image_to_cloudinary(new_image_path)
                    if new_image_url:
                        try:
                            conn_str = (
                                f"Driver={{ODBC Driver 18 for SQL Server}};"
                                f"Server={db_config['server']},1433;"  # Ensure port is included
                                f"Database={db_config['database']};"
                                f"UID={db_config['user']};"
                                f"PWD={db_config['password']};"
                                f"Authentication={db_config['authentication']};"
                            )
                            with closing(pyodbc.connect(conn_str)) as conn:
                                with conn.cursor() as cursor:
                                    cursor.execute(f"""
                                        UPDATE scooter_ev
                                        SET {column} = ?
                                        WHERE segment_id = ? AND model_type = ?
                                    """, (new_image_url, segment_id, model_type))
                                    conn.commit()

                                response["new_image_url"] = new_image_url
                                response["status"] = "Scratches detected, image updated"
                        except pyodbc.Error as e:
                            logging.error(f"Database error: {e}")
                            response["status"] = f"Database error: {str(e)}"
                else:
                    response["status"] = "No scratches detected, image retained"

                results.append(response)

        return jsonify(results), 200

    except Exception as e:
        logging.error(f"Error in upload_images endpoint: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)
