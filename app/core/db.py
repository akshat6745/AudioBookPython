import firebase_admin
from firebase_admin import credentials, firestore
import json
import os
import socket

def is_local_env():
    # You can enhance this check as needed
    hostname = socket.gethostname().lower()
    return (
        os.getenv("ENV") == "local" or  # You can explicitly set this in dev
        "localhost" in hostname or
        hostname.startswith("mac") or
        hostname.startswith("akshat") or
        os.path.exists("serviceAccountFirebase.json")  # fallback check
    )

# Initialize Firebase
if not firebase_admin._apps:
    cred = None
    try:
        if is_local_env():
            if os.path.exists("serviceAccountFirebase.json"):
                print("Initializing Firebase from local serviceAccountFirebase.json...")
                cred = credentials.Certificate("serviceAccountFirebase.json")
            elif os.getenv("FIREBASE_CREDENTIALS"):
                print("Initializing Firebase from FIREBASE_CREDENTIALS environment variable (local)...")
                cred = credentials.Certificate(json.loads(os.getenv("FIREBASE_CREDENTIALS")))
            else:
                print("Initializing Firebase using Application Default Credentials (local fallback)...")
                cred = credentials.ApplicationDefault()
        else:
            if os.getenv("FIREBASE_CREDENTIALS"):
                print("Initializing Firebase from FIREBASE_CREDENTIALS environment variable (prod)...")
                cred = credentials.Certificate(json.loads(os.getenv("FIREBASE_CREDENTIALS")))
            else:
                print("Initializing Firebase using Application Default Credentials (GCP)...")
                cred = credentials.ApplicationDefault()
    except Exception as init_err:
        # Final fallback to ADC if anything above fails
        print(f"Firebase explicit credentials failed ({init_err}); falling back to Application Default Credentials...")
        cred = credentials.ApplicationDefault()

    firebase_admin.initialize_app(cred)

db = firestore.client()
