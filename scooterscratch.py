import os
from flask import Flask, request, jsonify
import cloudinary
import cloudinary.uploader
import pymssql
import urllib
import logging
import cv2
import numpy as np
from dotenv import load_dotenv
from skimage.metrics import structural_similarity as ssim

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# Configure Cloudinary
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)

# Configure Database
db_config = {
    'server': os.getenv('AZURE_SQL_SERVER'),
    'database': os.getenv('AZURE_SQL_DATABASE'),
    'user': os.getenv('AZURE_SQL_USER'),
    'password': os.getenv('AZURE_SQL_PASSWORD'),
}

# Logger Configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# Function to retrieve image URL from the database
def retrieve_image_url_from_db(segment_id, model_type, column):
    try:
        with pymssql.connect(**db_config) as conn:
            with conn.cursor(as_dict=True) as cursor:
                query = f"""
                    SELECT scooter_id, segment_id, segment_name, model_type, {column}
                    FROM scooter_ev
                    WHERE model_type = %s AND segment_id = %s
                """
                cursor.execute(query, (model_type, segment_id))
                results = cursor.fetchall()

                return [
                    {
                        'scooter_id': row['scooter_id'],
                        'segment_id': row['segment_id'],
                        'segment_name': row['segment_name'],
                        'model_type': row['model_type'],
                        'image_url': row[column]
                    }
                    for row in results
                ]
    except pymssql.Error as e:
        logging.error(f"Database error: {e}")
        return []


# Function to upload images to Cloudinary
def upload_image_to_cloudinary(file):
    try:
        response = cloudinary.uploader.upload(file)
        return response['secure_url']
    except Exception as e:
        logging.error(f"Error uploading image to Cloudinary: {e}")
        return None


# Improved Image Comparison (Feature Matching with ORB)
def compare_images_for_similarity(new_image, existing_image):
    try:
        orb = cv2.ORB_create()
        kp1, des1 = orb.detectAndCompute(new_image, None)
        kp2, des2 = orb.detectAndCompute(existing_image, None)

        if des1 is None or des2 is None:
            return False  # No features detected

        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        matches = bf.match(des1, des2)

        similarity_score = len(matches) / max(len(kp1), len(kp2))
        return similarity_score > 0.7  # Adjust threshold as needed

    except Exception as e:
        logging.error(f"Error comparing images: {e}")
        return False


# Improved Scratch Detection for Minor Scratches
def detect_scratches(new_image, existing_image):
    try:
        # Convert images to grayscale if not already
        if len(new_image.shape) == 3:
            new_image = cv2.cvtColor(new_image, cv2.COLOR_BGR2GRAY)
        if len(existing_image.shape) == 3:
            existing_image = cv2.cvtColor(existing_image, cv2.COLOR_BGR2GRAY)

        # Resize images to match
        height, width = 500, 500
        new_image = cv2.resize(new_image, (width, height))
        existing_image = cv2.resize(existing_image, (width, height))

        # Compute absolute difference between images
        diff = cv2.absdiff(new_image, existing_image)

        # Normalize the difference for better visibility
        diff = cv2.normalize(diff, None, 0, 255, cv2.NORM_MINMAX)

        # Enhance contrast using histogram equalization
        diff = cv2.equalizeHist(diff)

        # Apply edge detection with lower thresholds for minor scratches
        edges = cv2.Canny(diff, 10, 100)  # Lower thresholds to detect lighter scratches

        # Morphological processing to enhance fine details
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 1))  # Smaller kernel for finer details
        morphed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

        # Find contours
        contours, _ = cv2.findContours(morphed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # Detect scratches based on size and shape
        scratch_detected = False
        for contour in contours:
            area = cv2.contourArea(contour)
            x, y, w, h = cv2.boundingRect(contour)
            aspect_ratio = w / h if h != 0 else 0

            # Adjust detection criteria for smaller and thinner scratches
            if 5 < area < 1500 and 0.5 < aspect_ratio < 10.0:  # Smaller area range for fine scratches
                scratch_detected = True
                break

        # Save result image for debugging (optional)
        result_image = cv2.drawContours(new_image.copy(), contours, -1, (255, 255, 255), 1)
        cv2.imwrite("scratch_detection_result.png", result_image)

        return scratch_detected

    except Exception as e:
        logging.error(f"Error detecting scratches: {e}")
        return False


# API Endpoint for Processing Images
@app.route('/process-images', methods=['POST'])
def process_images():
    try:
        model_type = request.form.get('model_type')
        segment_id = request.form.get('segment_id')
        files = request.files  # Uploaded images
        results = []

        if not all([model_type, segment_id, files]):
            return jsonify({"error": "Missing required parameters"}), 400

        for column, file in files.items():
            # Upload file to Cloudinary
            new_image_url = upload_image_to_cloudinary(file)

            if new_image_url:
                # Retrieve existing scooter image URLs from DB
                scooters = retrieve_image_url_from_db(segment_id, model_type, column)

                for scooter in scooters:
                    existing_image_url = scooter['image_url']
                    response = {"column": column}

                    if existing_image_url:
                        # Load images from URLs
                        with urllib.request.urlopen(new_image_url) as resp:
                            new_image_data = np.asarray(bytearray(resp.read()), dtype="uint8")
                        new_image = cv2.imdecode(new_image_data, cv2.IMREAD_GRAYSCALE)

                        with urllib.request.urlopen(existing_image_url) as resp:
                            existing_image_data = np.asarray(bytearray(resp.read()), dtype="uint8")
                        existing_image = cv2.imdecode(existing_image_data, cv2.IMREAD_GRAYSCALE)

                        # Compare images for similarity
                        is_similar = compare_images_for_similarity(new_image, existing_image)

                        if not is_similar:
                            response["status"] = "Images are different, no update made."
                        else:
                            # Detect scratches if images are similar
                            scratch_detected = detect_scratches(new_image, existing_image)

                            if scratch_detected:
                                # Update image in DB
                                try:
                                    with pymssql.connect(**db_config) as conn:
                                        with conn.cursor() as cursor:
                                            cursor.execute(f"""
                                                UPDATE scooter_ev
                                                SET {column} = %s
                                                WHERE segment_id = %s AND model_type = %s
                                            """, (new_image_url, segment_id, model_type))
                                            conn.commit()

                                        response["new_image_url"] = new_image_url
                                        response["status"] = "Scratches detected, image updated"
                                except pymssql.Error as e:
                                    logging.error(f"Database error: {e}")
                                    response["status"] = f"Database error: {str(e)}"
                            else:
                                response["status"] = "No scratches detected, image retained."
                    else:
                        response["status"] = "No existing image found, no update made."

                    results.append(response)

        return jsonify(results), 200

    except Exception as e:
        logging.error(f"Error in process_images endpoint: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
