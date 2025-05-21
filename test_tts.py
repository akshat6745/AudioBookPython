import requests
import json

def test_tts_endpoint():
    """
    Test the text-to-speech endpoint
    """
    url = "http://localhost:8001/tts"

    # Sample text to convert to speech
    data = {
        "text": "Hello, this is a test of the text to speech API using Edge TTS.",
        "voice": "en-US-ChristopherNeural"
    }

    # Make the request
    response = requests.post(url, json=data)

    # Check if the request was successful
    if response.status_code == 200:
        # Save the audio file
        with open("test_speech.mp3", "wb") as f:
            f.write(response.content)
        print("Success! Audio file saved as test_speech.mp3")
    else:
        print(f"Error: {response.status_code}")
        print(response.text)

if __name__ == "__main__":
    test_tts_endpoint()
