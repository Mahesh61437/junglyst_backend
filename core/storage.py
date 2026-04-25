import firebase_admin
from firebase_admin import credentials, storage
import uuid
import os
import time
import json
from django.conf import settings

# Initialize Firebase Admin once
if not firebase_admin._apps:
    service_account_info = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON')
    
    try:
        if service_account_info:
            # Load from environment variable (preferred for production/Railway)
            cred_dict = json.loads(service_account_info)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
        else:
            # Fallback to local file for development
            service_account_path = os.path.join(settings.BASE_DIR, 'firebase_service_account.json')
            if os.path.exists(service_account_path):
                cred = credentials.Certificate(service_account_path)
                firebase_admin.initialize_app(cred)
            else:
                print("WARNING: Firebase service account credentials not found. Image uploads will fail.")
    except Exception as e:
        print(f"CRITICAL: Failed to initialize Firebase Admin: {str(e)}")

def upload_to_firebase(file_obj, user_id, type_prefix="asset"):
    """
    Uploads a file to Firebase Storage with a professional folder structure.
    Bucket: Junglyst
    Structure: sellers/{user_id}/{type_prefix}/{filename}_{timestamp}.{ext}
    """
    # Use the specifically requested bucket name
    bucket = storage.bucket("Junglyst")
    
    # Extract extension and clean type prefix
    ext = file_obj.name.split('.')[-1]
    timestamp = int(time.time())
    
    # Format a professional filename using the type and a unique hash
    # Convention: sellers/ID/logo/logo_123456789.png
    clean_type = type_prefix.lower().replace(' ', '_')
    unique_id = uuid.uuid4().hex[:8]
    filename = f"sellers/{user_id}/{clean_type}/{clean_type}_{timestamp}_{unique_id}.{ext}"
    
    blob = bucket.blob(filename)
    blob.upload_from_file(file_obj, content_type=file_obj.content_type)
    
    # Make the blob public for frontend access
    blob.make_public()
    return blob.public_url
