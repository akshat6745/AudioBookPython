import firebase_admin
from firebase_admin import credentials, firestore
import json
import os
from app.core.settings import settings

# Initialize Firebase
if not firebase_admin._apps:
    cred = None
    try:
        if settings.is_local:
            if os.path.exists("serviceAccountFirebase.json"):
                print("Initializing Firebase from local serviceAccountFirebase.json...")
                cred = credentials.Certificate("serviceAccountFirebase.json")
            elif settings.FIREBASE_CREDENTIALS:
                print("Initializing Firebase from FIREBASE_CREDENTIALS environment variable (local)...")
                cred = credentials.Certificate(json.loads(settings.FIREBASE_CREDENTIALS))
            else:
                print("Initializing Firebase using Application Default Credentials (local fallback)...")
                cred = credentials.ApplicationDefault()
        else:
            if settings.FIREBASE_CREDENTIALS:
                print("Initializing Firebase from FIREBASE_CREDENTIALS environment variable (prod)...")
                cred = credentials.Certificate(json.loads(settings.FIREBASE_CREDENTIALS))
            else:
                print("Initializing Firebase using Application Default Credentials (GCP)...")
                cred = credentials.ApplicationDefault()
    except Exception as init_err:
        # Final fallback to ADC if anything above fails
        print(f"Firebase explicit credentials failed ({init_err}); falling back to Application Default Credentials...")
        cred = credentials.ApplicationDefault()

    firebase_admin.initialize_app(cred)

db = firestore.client()
