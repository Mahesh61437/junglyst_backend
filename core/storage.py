import firebase_admin
from firebase_admin import credentials, storage
import uuid
import os
import time
import json
from decouple import config
from django.conf import settings

# Initialize Firebase Admin once
if not firebase_admin._apps:
    service_account_info = config('FIREBASE_SERVICE_ACCOUNT_JSON', default=None)

    try:
        if service_account_info:
            cred_dict = json.loads(service_account_info)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
        else:
            service_account_path = os.path.join(settings.BASE_DIR, 'firebase_service_account.json')
            if os.path.exists(service_account_path):
                cred = credentials.Certificate(service_account_path)
                firebase_admin.initialize_app(cred)
            else:
                print("WARNING: Firebase service account credentials not found. Image uploads will fail.")
    except Exception as e:
        print(f"CRITICAL: Failed to initialize Firebase Admin: {str(e)}")


# Profile asset types managed under sellers/{user_id}/profile/
_PROFILE_TYPES = {'logo', 'banner', 'avatar'}


def _build_path(user_id: str, type_prefix: str) -> str:
    """
    Returns a Firebase Storage path that is human-readable, env-scoped,
    and cleanly separable for future migrations.

    Seller profile assets:
        {env}/sellers/{user_id}/profile/{type}/{type}_{ts}_{uid}.{ext}
        e.g. prod/sellers/abc123/profile/logo/logo_1714900000_a1b2c3d4.jpg

    Product / listing images:
        {env}/sellers/{user_id}/products/{ts}_{uid}.{ext}
        e.g. prod/sellers/abc123/products/1714900000_a1b2c3d4.jpg

    Other assets (future use):
        {env}/assets/{type}/{ts}_{uid}.{ext}
    """
    env = 'prod' if config('RAILWAY_ENVIRONMENT_NAME', default='') == 'production' else 'dev'
    ts = int(time.time())
    uid = uuid.uuid4().hex[:8]
    # extension placeholder — caller appends it
    clean = type_prefix.lower().strip()

    if clean in _PROFILE_TYPES:
        return f"{env}/sellers/{user_id}/profile/{clean}/{clean}_{ts}_{uid}"
    elif clean == 'product':
        return f"{env}/sellers/{user_id}/products/{ts}_{uid}"
    else:
        return f"{env}/assets/{clean}/{ts}_{uid}"


def upload_to_firebase(file_obj, user_id, type_prefix="asset"):
    """
    Uploads a file to Firebase Storage.

    Path conventions (see _build_path for details):
      - logo / banner / avatar  →  {env}/sellers/{user_id}/profile/{type}/...
      - product                 →  {env}/sellers/{user_id}/products/...
      - other                   →  {env}/assets/{type}/...
    """
    bucket_name = config('FIREBASE_STORAGE_BUCKET')
    try:
        bucket = storage.bucket(bucket_name)
    except Exception:
        bucket = storage.bucket()

    ext = file_obj.name.rsplit('.', 1)[-1].lower()
    base_path = _build_path(str(user_id), type_prefix)
    filename = f"{base_path}.{ext}"

    blob = bucket.blob(filename)
    blob.upload_from_file(file_obj, content_type=file_obj.content_type)
    blob.make_public()
    return blob.public_url
