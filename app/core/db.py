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
    if is_local_env():
        print("Initializing Firebase from local serviceAccountFirebase.json...")
        cred = credentials.Certificate("serviceAccountFirebase.json")
    else:
        print("Initializing Firebase from FIREBASE_CREDENTIALS environment variable...")
        firebase_creds = os.getenv("FIREBASE_CREDENTIALS")
        cred = credentials.Certificate(json.loads(firebase_creds))

    firebase_admin.initialize_app(cred)

db = firestore.client()
