# Home Voice

A separate one-button browser app for smart-home-service. Tap to start recording,
then tap again to send. The app also sends automatically after 15 seconds.
OpenAI transcribes the audio and selects tools from the registered device catalog.
Ask for device status, saved device or channel aliases, supported controls, or
changes such as power, level, color, mode, or a timer. The app displays the transcript
and backend result and reads the result aloud in browsers that support speech synthesis.

Commands use the selected device's effective definitions. Local controls accept
mapped Boolean, numeric, string/enum, and encoded HSV values. Tuya Sharing controls
accept advertised Boolean, integer, and enum values. The app validates ranges,
steps, and choices, then the backend validates and encodes the request again.
Raw payloads and vendor-specific schedules and scenes are outside the voice tools.
An accepted command is reported separately from a live status reading.

## Run locally

From this repository root, install
`python -m pip install -r requirements.txt` in your environment.
Run the smart-home backend separately. Set these environment variables before
starting the voice app.

| Variable | Value |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI project API key |
| `VOICE_PASSWORD` | A unique password for the voice page |
| `VOICE_DEVICES` | JSON object mapping allowed numeric inventory IDs to voice names, e.g. `{"12":"kitchen light","14":"workroom switch"}`. Falls back to the legacy `VOICE_LIGHTS` setting. |
| `VOICE_CLOUD_IDS` | Optional comma-separated subset of those IDs to route through the connected Tuya Sharing account. Other devices use the local API. |
| `VOICE_BACKEND_URL` | Backend base URL, defaults to `http://127.0.0.1:8000` |
| `SMART_HOME_API_TOKEN` | Backend API token, if configured |
| `VOICE_MODEL` | Optional function-calling model override |

Use IDs from the backend's `GET /api/v1/devices` response. Devices without
capability mappings can still be named and looked up. Channel aliases come from
the connected Tuya Sharing catalog and resolve to their specific `switch_N` code.

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

## Deploy to Docker Swarm

Run these steps on the Swarm manager. Label the node that should host the LAN
endpoint. Its address must match the TLS certificate, and its firewall should
allow TCP 8011 from your home network.

```sh
docker node update --label-add home_voice=true YOUR_NODE
```

Create the five Swarm secrets from protected files outside this repository.
Use the backend API token already configured in smart-home-service. If the
backend uses no token, supply an empty line as the token file. The certificate
file should contain the certificate chain trusted by your phone and laptop.

```sh
docker secret create home_voice_openai_key /path/to/openai-key
docker secret create home_voice_backend_token /path/to/backend-token
docker secret create home_voice_password /path/to/voice-password
docker secret create home_voice_tls_cert /path/to/fullchain.pem
docker secret create home_voice_tls_key /path/to/privkey.pem
```

For a single-node Swarm, build the image on that node. Use a new tag for each
update. Set the backend URL to an address reachable from the voice container,
such as the existing smart-home-service Docker frontend's LAN address on
port 8080. A container's loopback address only reaches that container.

```sh
export VOICE_IMAGE=smart-home-voice:v1
export VOICE_BACKEND_URL=http://192.168.1.20:8080
export VOICE_LIGHTS='{"12":"living room ceiling light"}'
docker build -t "$VOICE_IMAGE" .
docker stack config -c swarm.yaml
docker stack deploy --resolve-image never -c swarm.yaml home-voice
docker stack services home-voice
```

For a multi-node Swarm, build for the target node's architecture and push the
tag to a registry that node can reach. Deploy with the registry-qualified
`VOICE_IMAGE` and use `--with-registry-auth` if the registry is private.

Open `https://YOUR_VOICE_HOST:8011`, sign in with username `voice`, and grant
microphone access. Check a configured light with an on or off command.

## Deploy with Compose on the Dell

The Compose file uses Linux host networking to reach the existing backend at
`127.0.0.1:8000`. Build the image with `docker build -t "$VOICE_IMAGE" .`.
Set `VOICE_IMAGE`, `VOICE_DEVICES`, and `VOICE_SECRETS_DIR` before running
`docker compose up -d --wait`.

Put `voice.crt`, `voice.key`, `password`, `openai-key`, and `backend-token`
in the secrets directory. The container runs as UID/GID 10001, which needs read
access to the files and search access to the directory. Keep the TLS CA private
key outside that directory. Open the Dell's HTTPS address on port 8011.
