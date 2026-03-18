# pipecat-wavix

`pipecat-wavix` Serializer for Wavix WebSocket protocol.
This serializer handles bidirectional audio convertion between Pipecat frames and Wavix WebSocket media stream protocol.
Wavix media contract is outlined below:
- Format: **pcm16**
- Sample rate: **24 kHz**
- Bit depth: **16-bit**
- Frame size: **20ms**
- Channels: **Mono**
- Endianness: **Little-endian**


# Prerequisites

### Wavix account
Before using the Wavix Frame serializer for Pipecat, you'll need:
- An active [Wavix account](https://docs.wavix.com/getting-started/create-account)
- A Wavix phone number

### Evironment variables
- `WAVIX_API_KEY` - [Wavix API key](https://docs.wavix.com/api-reference/authentication)
- `OPENAI_API_KEY` - OpenAI API key
- `DEEPGRAM_API_KEY` - Deepgram API key
- `CARTESIA_API_KEY` - Cartesia API key

## System
- Python 3.12
- `uv` package manager
- ngrok (for local development)

# Setup
Install WavixFrameSerializer

```bash
pip install pipecat-wavix
```

If your prefer `uv`, update your pyproject.toml:
```bash
[project]
name = "your-project"
version = "0.1.0"
dependencies = [
    "pipecat-wavix>=1.0.0",
    "Any other packages"
]
```
Set up a virtual environment and install dependencies:
```bash
uv sync
```

## Usage with Pipecat Pipeline

Import WavixFrameSerializer to your project
```python
from pipecat_wavix import WavixFrameSerializer
```

```python
WAVIX_SAMPLE_RATE = 24000
BOT_SAMPLE_RATE = 16000

# ── Serializer ─────────────────────────────────────────────────────────────
serializer = WavixFrameSerializer(
    stream_id=stream_id,
    params=WavixFrameSerializer.InputParams(
        wavix_sample_rate=WAVIX_SAMPLE_RATE,
        sample_rate=BOT_SAMPLE_RATE,
        audio_track="inbound",
    ),
)    
# ── Transport ──────────────────────────────────────────────────────────────
"""
Outbound audio is normalized to Wavix's required format:
PCM16, 24 kHz, mono, little-endian, 20 ms frames.
"""
transport = FastAPIWebsocketTransport(
    websocket=websocket,
    params=FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_in_passthrough=True,
        audio_in_sample_rate=BOT_SAMPLE_RATE,
        audio_out_enabled=True,
        audio_out_sample_rate=WAVIX_SAMPLE_RATE,
        add_wav_header=False,
        serializer=serializer,
        audio_out_10ms_chunks=2,
        fixed_audio_packet_size=960,
    ),
)
```

# Running the example
## Recommended Way: Run `server.py`
Use `server.py` if you want the full Wavix call flow handled automatically.

### What `server.py` does

1. Starts FastAPI locally.
2. Opens an ngrok tunnel automatically.
3. Exposes:
   - `POST /wavix/inbound` for the Wavix webhook
   - `GET /health` for health checks
   - `WS /ws` for Wavix media streaming
4. When Wavix sends the inbound webhook, it:
   - answers the call
   - starts bidirectional media streaming to `/ws`
5. When Wavix connects to `/ws`, the server launches the bot from `bot.py`

### Command

```bash
uv run server.py
```

### What to expect

The server will print values like:

- local URL: `http://localhost:7860`
- public ngrok URL
- webhook URL: `https://.../wavix/inbound`
- websocket URL: `wss://.../ws`

### Wavix configuration

Configure your Wavix number's voice webhook to the printed webhook URL.

```text
https://your-public-url/wavix/inbound
```

1. Log in to your Wavix account.
2. Navigate to **Numbers & trunks** → **My numbers**.
3. Select your number by clicking the ⋯ menu → **Edit number**.
4. Configure the **Voice webhook** by pasting the printed ngrok's *Webhook* URL and click **Save**.


After that, place a call to the number. The server handles the rest.

### Important notes

- Do not start ngrok manually for this mode.
- Do not manually call the Wavix answer/stream APIs in this mode. `server.py` already does that.

## Standalone Bot Mode: Run `bot.py`

Use `bot.py` only if you want to run the Pipecat development runner and you are prepared to manage Wavix call answer/stream setup separately.

### What `bot.py` does

- Starts Pipecat's development runner.
- Accepts a telephony WebSocket connection.
- Parses the initial Wavix handshake from the WebSocket.
- Builds the Pipecat transport and audio pipeline.

### What `bot.py` does not do

- It does not call the Wavix REST API to answer the call.
- It does not create the Wavix stream for you.
- It does not replace `server.py` as a full inbound call orchestrator.

### Command

Run the Pipecat development runner:

```bash
uv run bot.py --transport wavix --proxy your-public-hostname
```

Example:

```bash
uv run bot.py --transport wavix --proxy semiprovincial-forwardly-eloy.ngrok-free.dev
```

### Public exposure

In this mode, start ngrok in another terminal:

```bash
ngrok http 7860
```

Use the hostname from ngrok as the `--proxy` value. Do not include `https://` in the `--proxy` argument unless Pipecat specifically requires it in your environment.

### Wavix setup for standalone bot mode

Because `bot.py` only handles the media WebSocket, you must answer the call and start streaming programmatically.

Typical sequence:

1. Receive or know the `call_id`. You can configure your Wavix number's voice webhook as described earlier to receive the [on-call events](https://docs.wavix.com/api-reference/call-webhooks/on-call-event).
2. Answer the call through Wavix:

```bash
curl -L "https://api.wavix.com/v1/calls/$CALL_ID/answer" \
  -H "Authorization: Bearer $WAVIX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'
```

3. Start the media stream to the Pipecat runner WebSocket:

```bash
curl -L "https://api.wavix.com/v1/calls/$CALL_ID/streams" \
  -H "Authorization: Bearer $WAVIX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "stream_url": "wss://your-public-hostname/ws",
    "stream_type": "twoway",
    "stream_channel": "inbound"
  }'
```

### When to use `bot.py`

- Use it for local Pipecat runner testing.
- Use it if another service handles the Wavix webhook and REST orchestration.
- Do not use it as the primary inbound call entrypoint unless you have that external orchestration.
