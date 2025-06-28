# TTS Dual-Voice API Guide for UI Components

## Overview
The TTS Dual-Voice API allows you to convert text to speech using two different voices - one for narrative/paragraph content and another for dialogue. This creates a more engaging audiobook experience.

## API Endpoints

### 1. Direct TTS Dual-Voice
**POST** `/tts-dual-voice`

Converts text to speech using dual voices for paragraphs and dialogue.

#### Request Body
```json
{
    "text": "The hero walked into the room. \"Hello there,\" he said confidently.",
    "paragraphVoice": "en-US-ChristopherNeural",
    "dialogueVoice": "en-US-AriaNeural"
}
```

#### Response
- **Content-Type**: `audio/mp3`
- **Headers**: 
  - `Content-Disposition: attachment; filename=speech.mp3`
  - `Cache-Control: no-cache`

### 2. Novel Chapter with TTS
**GET** `/novel-with-tts`

Fetches a novel chapter and converts it to speech with dual voices.

#### Query Parameters
- `novelName` (string, required): Name of the novel
- `chapterNumber` (integer, required): Chapter number to fetch
- `voice` (string, required): Voice for narrative/paragraph content
- `dialogueVoice` (string, required): Voice for dialogue content

#### Example Request
```
GET /novel-with-tts?novelName=sample-novel&chapterNumber=1&voice=en-US-ChristopherNeural&dialogueVoice=en-US-AriaNeural
```

#### Response
- **Content-Type**: `audio/mp3`
- **Headers**: 
  - `Content-Disposition: attachment; filename=chapter_{chapterNumber}.mp3`
  - `Cache-Control: no-cache`

## Available Voices

### Male Voices (Good for Narrative)
- `en-US-ChristopherNeural` - Deep, authoritative
- `en-US-EricNeural` - Warm, friendly
- `en-US-GuyNeural` - Clear, professional
- `en-US-RogerNeural` - Mature, storytelling

### Female Voices (Good for Dialogue)
- `en-US-AriaNeural` - Natural, expressive
- `en-US-JennyNeural` - Clear, versatile
- `en-US-MichelleNeural` - Warm, engaging
- `en-US-SaraNeural` - Smooth, pleasant

### Additional Voices
- `en-US-DavisNeural` - Professional male
- `en-US-JaneNeural` - Friendly female
- `en-US-JasonNeural` - Casual male
- `en-US-NancyNeural` - Mature female

## UI Implementation Examples

### 1. React Component Example

```jsx
import React, { useState } from 'react';

const TTSPlayer = () => {
    const [isLoading, setIsLoading] = useState(false);
    const [audioUrl, setAudioUrl] = useState(null);
    
    const generateTTS = async (text, paragraphVoice, dialogueVoice) => {
        setIsLoading(true);
        try {
            const response = await fetch('/api/tts-dual-voice', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    text: text,
                    paragraphVoice: paragraphVoice,
                    dialogueVoice: dialogueVoice
                })
            });
            
            if (response.ok) {
                const audioBlob = await response.blob();
                const url = URL.createObjectURL(audioBlob);
                setAudioUrl(url);
            }
        } catch (error) {
            console.error('TTS Error:', error);
        } finally {
            setIsLoading(false);
        }
    };

    const generateChapterAudio = async (novelName, chapterNumber, voice, dialogueVoice) => {
        setIsLoading(true);
        try {
            const params = new URLSearchParams({
                novelName,
                chapterNumber: chapterNumber.toString(),
                voice,
                dialogueVoice
            });
            
            const response = await fetch(`/api/novel-with-tts?${params}`);
            
            if (response.ok) {
                const audioBlob = await response.blob();
                const url = URL.createObjectURL(audioBlob);
                setAudioUrl(url);
            }
        } catch (error) {
            console.error('Chapter TTS Error:', error);
        } finally {
            setIsLoading(false);
        }
    };

    return (
        <div>
            {isLoading && <div>Generating audio...</div>}
            {audioUrl && (
                <audio controls>
                    <source src={audioUrl} type="audio/mp3" />
                    Your browser does not support the audio element.
                </audio>
            )}
        </div>
    );
};

export default TTSPlayer;
```

### 2. JavaScript/Fetch Example

```javascript
class TTSService {
    constructor(baseUrl = '/api') {
        this.baseUrl = baseUrl;
    }

    async generateDualVoiceTTS(text, paragraphVoice = 'en-US-ChristopherNeural', dialogueVoice = 'en-US-AriaNeural') {
        try {
            const response = await fetch(`${this.baseUrl}/tts-dual-voice`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    text,
                    paragraphVoice,
                    dialogueVoice
                })
            });

            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }

            return await response.blob();
        } catch (error) {
            console.error('TTS generation failed:', error);
            throw error;
        }
    }

    async generateChapterAudio(novelName, chapterNumber, voice = 'en-US-ChristopherNeural', dialogueVoice = 'en-US-AriaNeural') {
        try {
            const params = new URLSearchParams({
                novelName,
                chapterNumber: chapterNumber.toString(),
                voice,
                dialogueVoice
            });

            const response = await fetch(`${this.baseUrl}/novel-with-tts?${params}`);

            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }

            return await response.blob();
        } catch (error) {
            console.error('Chapter audio generation failed:', error);
            throw error;
        }
    }
}

// Usage
const ttsService = new TTSService();

// Generate TTS for custom text
ttsService.generateDualVoiceTTS(
    'The wizard raised his staff. "You shall not pass!" he declared.',
    'en-US-ChristopherNeural',
    'en-US-AriaNeural'
).then(audioBlob => {
    const audioUrl = URL.createObjectURL(audioBlob);
    const audio = new Audio(audioUrl);
    audio.play();
});

// Generate TTS for a novel chapter
ttsService.generateChapterAudio(
    'sample-novel',
    1,
    'en-US-ChristopherNeural',
    'en-US-AriaNeural'
).then(audioBlob => {
    const audioUrl = URL.createObjectURL(audioBlob);
    const audio = new Audio(audioUrl);
    audio.play();
});
```

### 3. Voice Selection Component

```jsx
const VoiceSelector = ({ onVoiceChange, selectedVoice, voiceType }) => {
    const voices = {
        male: [
            { value: 'en-US-ChristopherNeural', label: 'Christopher (Deep, Authoritative)' },
            { value: 'en-US-EricNeural', label: 'Eric (Warm, Friendly)' },
            { value: 'en-US-GuyNeural', label: 'Guy (Clear, Professional)' },
            { value: 'en-US-RogerNeural', label: 'Roger (Mature, Storytelling)' },
        ],
        female: [
            { value: 'en-US-AriaNeural', label: 'Aria (Natural, Expressive)' },
            { value: 'en-US-JennyNeural', label: 'Jenny (Clear, Versatile)' },
            { value: 'en-US-MichelleNeural', label: 'Michelle (Warm, Engaging)' },
            { value: 'en-US-SaraNeural', label: 'Sara (Smooth, Pleasant)' },
        ]
    };

    return (
        <select 
            value={selectedVoice} 
            onChange={(e) => onVoiceChange(e.target.value)}
            className="voice-selector"
        >
            <option value="">Select {voiceType} Voice</option>
            {voices.male.map(voice => (
                <optgroup key="male" label="Male Voices">
                    <option key={voice.value} value={voice.value}>
                        {voice.label}
                    </option>
                </optgroup>
            ))}
            {voices.female.map(voice => (
                <optgroup key="female" label="Female Voices">
                    <option key={voice.value} value={voice.value}>
                        {voice.label}
                    </option>
                </optgroup>
            ))}
        </select>
    );
};
```

## Text Processing Features

The API automatically handles:

1. **Dialogue Detection**: Text within quotes (`"..."`) is processed with the dialogue voice
2. **Narrative Processing**: Text outside quotes uses the paragraph voice
3. **Text Cleaning**: Removes unwanted patterns and website references
4. **Special Character Handling**: Converts symbols like `***` to spoken equivalents
5. **Empty Text Handling**: Generates appropriate silence for empty or whitespace-only content

## Error Handling

Common error scenarios and status codes:

- `400 Bad Request`: Invalid request body or missing required fields
- `500 Internal Server Error`: TTS processing failed or chapter not found

Example error response:
```json
{
    "detail": "Error in dual-voice text-to-speech conversion: ..."
}
```

## Performance Considerations

1. **Audio File Size**: Generated MP3 files can be large for long texts
2. **Processing Time**: TTS generation takes time proportional to text length
3. **Caching**: Consider implementing client-side caching for generated audio
4. **Progress Indication**: Show loading states for better UX

## Best Practices

1. **Voice Pairing**: Use contrasting voices (e.g., male narrator + female dialogue)
2. **Text Length**: For long chapters, consider breaking into smaller segments
3. **User Preferences**: Allow users to save their preferred voice combinations
4. **Error Handling**: Implement retry logic for failed requests
5. **Audio Controls**: Provide standard audio controls (play, pause, seek, volume)

## Example Complete Implementation

```html
<!DOCTYPE html>
<html>
<head>
    <title>TTS Dual Voice Player</title>
</head>
<body>
    <div id="tts-player">
        <h3>Text to Speech with Dual Voices</h3>
        
        <div>
            <label>Narrator Voice:</label>
            <select id="narratorVoice">
                <option value="en-US-ChristopherNeural">Christopher (Deep, Authoritative)</option>
                <option value="en-US-EricNeural">Eric (Warm, Friendly)</option>
            </select>
        </div>
        
        <div>
            <label>Dialogue Voice:</label>
            <select id="dialogueVoice">
                <option value="en-US-AriaNeural">Aria (Natural, Expressive)</option>
                <option value="en-US-JennyNeural">Jenny (Clear, Versatile)</option>
            </select>
        </div>
        
        <div>
            <textarea id="textInput" placeholder="Enter text with dialogue in quotes..."></textarea>
        </div>
        
        <button onclick="generateAudio()">Generate Audio</button>
        
        <div id="audioPlayer" style="display: none;">
            <audio controls id="audioElement"></audio>
        </div>
        
        <div id="loading" style="display: none;">Generating audio...</div>
    </div>

    <script>
        async function generateAudio() {
            const text = document.getElementById('textInput').value;
            const narratorVoice = document.getElementById('narratorVoice').value;
            const dialogueVoice = document.getElementById('dialogueVoice').value;
            
            if (!text.trim()) {
                alert('Please enter some text');
                return;
            }
            
            document.getElementById('loading').style.display = 'block';
            document.getElementById('audioPlayer').style.display = 'none';
            
            try {
                const response = await fetch('/api/tts-dual-voice', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        text: text,
                        paragraphVoice: narratorVoice,
                        dialogueVoice: dialogueVoice
                    })
                });
                
                if (response.ok) {
                    const audioBlob = await response.blob();
                    const audioUrl = URL.createObjectURL(audioBlob);
                    
                    const audioElement = document.getElementById('audioElement');
                    audioElement.src = audioUrl;
                    
                    document.getElementById('audioPlayer').style.display = 'block';
                } else {
                    alert('Failed to generate audio');
                }
            } catch (error) {
                console.error('Error:', error);
                alert('An error occurred while generating audio');
            } finally {
                document.getElementById('loading').style.display = 'none';
            }
        }
    </script>
</body>
</html>
```

This comprehensive guide provides everything your UI components need to effectively use the TTS dual-voice API, including practical examples and best practices.
