import firebase_admin
from firebase_admin import credentials, storage
import uuid
import os
import time
from django.conf import settings

# Initialize Firebase Admin once
if not firebase_admin._apps:
    service_account_path = os.path.join(settings.BASE_DIR, 'firebase_service_account.json')
    cred = credentials.Certificate(service_account_path)
    # The user specifically asked for bucket "Junglyst"
    # However, usually it's bucket-name.appspot.com
    # I'll check settings first, but override if needed
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
