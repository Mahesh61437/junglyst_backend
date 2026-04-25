import requests
from django.conf import settings
from django.core.cache import cache

class NimbuspostService:
    BASE_URL = "https://api.nimbuspost.com/v1"

    @classmethod
    def get_token(cls):
        token = cache.get('nimbuspost_token')
        if token:
            return token
        
        url = f"{cls.BASE_URL}/users/login"
        payload = {
            "email": settings.NIMBUSPOST_EMAIL,
            "password": settings.NIMBUSPOST_PASSWORD
        }
        
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            data = response.json()
            if data.get('status'):
                token = data.get('data')
                # Cache for 23 hours (assuming 24h validity)
                cache.set('nimbuspost_token', token, 3600 * 23)
                return token
        return None

    @classmethod
    def check_serviceability(cls, origin_pincode, destination_pincode, weight):
        token = cls.get_token()
        if not token:
            return None
        
        url = f"{cls.BASE_URL}/courier/serviceability"
        params = {
            "origin": origin_pincode,
            "destination": destination_pincode,
            "weight": weight
        }
        headers = {"Authorization": f"Bearer {token}"}
        
        response = requests.get(url, params=params, headers=headers)
        if response.status_code == 200:
            return response.json()
        return None

    @classmethod
    def create_shipment(cls, shipment_data):
        token = cls.get_token()
        if not token:
            return None
        
        url = f"{cls.BASE_URL}/shipments"
        headers = {"Authorization": f"Bearer {token}"}
        
        response = requests.post(url, json=shipment_data, headers=headers)
        return response.json() if response.status_code == 200 else None

    @classmethod
    def generate_label(cls, shipment_id):
        token = cls.get_token()
        if not token:
            return None
        
        url = f"{cls.BASE_URL}/shipments/label"
        params = {"shipment_id": shipment_id}
        headers = {"Authorization": f"Bearer {token}"}
        
        response = requests.get(url, params=params, headers=headers)
        if response.status_code == 200:
            data = response.json()
            # Nimbuspost often returns a URL to the PDF label
            return data.get('data', {}).get('label_url')
        return None

    @classmethod
    def schedule_pickup(cls, shipment_id, pickup_date):
        token = cls.get_token()
        if not token:
            return None
        
        url = f"{cls.BASE_URL}/shipments/pickup"
        payload = {
            "shipment_id": shipment_id,
            "pickup_date": pickup_date
        }
        headers = {"Authorization": f"Bearer {token}"}
        
        response = requests.post(url, json=payload, headers=headers)
        return response.json() if response.status_code == 200 else None

    @classmethod
    def track_shipment(cls, awb_number):
        token = cls.get_token()
        if not token:
            return None
        
        url = f"{cls.BASE_URL}/shipments/track/{awb_number}"
        headers = {"Authorization": f"Bearer {token}"}
        
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
        return None
