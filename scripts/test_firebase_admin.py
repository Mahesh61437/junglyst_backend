import firebase_admin
from firebase_admin import credentials, storage
import os
from decouple import config

def test_firebase_admin():
    print("\n--- Testing Firebase Admin SDK ---")
    service_account_path = "firebase_service_account.json"
    bucket_name = config('FIREBASE_STORAGE_BUCKET')
    
    if not os.path.exists(service_account_path):
        print(f"FAILED: Service account file not found at {service_account_path}")
        return False
        
    try:
        cred = credentials.Certificate(service_account_path)
        firebase_admin.initialize_app(cred, {
            'storageBucket': bucket_name
        })
        
        bucket = storage.bucket()
        # Try to list some files (even if empty)
        blobs = list(bucket.list_blobs(max_results=1))
        print(f"SUCCESS: Firebase Admin SDK initialized. Bucket '{bucket_name}' is accessible.")
        return True
    except Exception as e:
        print(f"FAILED: Firebase Admin check failed: {str(e)}")
    return False

if __name__ == "__main__":
    test_firebase_admin()
