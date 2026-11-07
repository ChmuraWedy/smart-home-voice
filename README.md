# Home Voice

A separate one-button browser app for smart-home-service. Tap to start recording,
then tap again to send. The app also sends automatically after 15 seconds.
OpenAI transcribes the audio and returns a function call. The app can only turn
configured lights on or off. It displays the transcript and the backend result.

## Run locally

From this repository root, install
`python -m pip install -r requirements.txt` in your environment.
Run the smart-home backend separately. Set these environment variables before
starting the voice app.

| Variable | Value |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI project API key |
| `VOICE_PASSWORD` | A unique password for the voice page |
| `VOICE_LIGHTS` | JSON object mapping existing numeric device IDs to clear room and light names, e.g. `{"12":"living room ceiling light","14":"living room floor lamp"}` |
| `VOICE_BACKEND_URL` | Backend base URL, defaults to `http://127.0.0.1:8000` |
| `SMART_HOME_API_TOKEN` | Backend API token, if configured |
| `VOICE_MODEL` | Optional function-calling model override |

Use device IDs from the existing backend's `GET /api/v1/devices` response.
Only include lights that support the backend's on and off commands.

```sh
python -m uvicorn app:app --host 127.0.0.1 --port 8011
```

Open http://localhost:8011 on the server machine. When the browser prompts,
enter `voice` as the username and your configured password. Allow microphone
access when prompted.

## Phone and laptop on Wi-Fi

Serve the voice app over HTTPS with a certificate both devices trust. For
example, keep the process on loopback and place your LAN HTTPS reverse proxy
in front of it. Open the proxy's HTTPS address on each device. The site and
backend can stay on the LAN. Audio, the transcript, and configured light
labels are sent to OpenAI, so voice commands require internet access.
