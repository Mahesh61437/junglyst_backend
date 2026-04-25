import requests
import os
from decouple import config

def test_nimbuspost_login():
    print("\n--- Testing Nimbuspost LOGIN API ---")
    email = config('NIMBUSPOST_EMAIL')
    password = config('NIMBUSPOST_PASSWORD')
    
    # Trying the users/login endpoint which is more common in their newer docs
    url = "https://api.nimbuspost.com/v1/users/login"
    payload = {
        "email": email,
        "password": password
    }
    
    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            data = response.json()
            if data.get('status'):
                print("SUCCESS: Nimbuspost Login successful.")
                print(f"Token: {data.get('data')[:10]}...")
                return True
            else:
                print(f"FAILED: Nimbuspost status False: {data.get('message')}")
        else:
            print(f"FAILED: Nimbuspost status {response.status_code}")
            print(response.text)
    except Exception as e:
        print(f"ERROR: {str(e)}")
    return False

def test_firebase_public():
    print("\n--- Testing Firebase Storage Public Access ---")
    bucket = config('FIREBASE_STORAGE_BUCKET')
    # Trying common bucket formats
    urls = [
        f"https://firebasestorage.googleapis.com/v0/b/{bucket}",
        f"https://firebasestorage.googleapis.com/v0/b/aqua-india-61437.appspot.com"
    ]
    
    for url in urls:
        try:
            response = requests.get(url)
            # If it exists, it should return 401/403 or 200 (if public)
            if response.status_code in [200, 401, 403]:
                print(f"SUCCESS: Firebase Storage reachable at {url} (Status: {response.status_code})")
                return True
        except:
            pass
    print("FAILED: Firebase Storage not reachable with tested formats.")
    return False

if __name__ == "__main__":
    nimbus_ok = test_nimbuspost_login()
    firebase_ok = test_firebase_public()
    
    print("\n--- Summary ---")
    print(f"Nimbuspost: {'WORKING' if nimbus_ok else 'FAILED'}")
    print(f"Firebase:   {'WORKING' if firebase_ok else 'FAILED'}")
