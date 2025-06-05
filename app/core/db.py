import firebase_admin
from firebase_admin import credentials, firestore
import json
import os

# Fetch the secret from an environment variable
firebase_creds = os.getenv("FIREBASE_CREDENTIALS")
if not firebase_admin._apps:
    cred = credentials.Certificate(json.loads(firebase_creds))
    firebase_admin.initialize_app(cred)

db = firestore.client()