# pipecat-wavix

Stream real-time audio between Pipecat and Wavix using WebSockets.

`pipecat-wavix` converts audio between Pipecat frames and the Wavix media stream format so you can build voice-enabled applications quickly.

---

## What this project does

This repository contains the source code for the Wavix Frame Serializer for Pipecat.

It lets you stream audio between Pipecat and Wavix by converting audio data between their formats in real time.

---

## What’s included

This project includes:

* **WavixFrameSerializer**
  Converts audio between Pipecat frames and the Wavix WebSocket media stream format.

* **bot.py**
  A minimal example that runs a Pipecat development bot and connects to a Wavix media stream.
  Use this if you want full control over call handling.

* **server.py**
  A complete example that handles the full Wavix call flow, including webhooks, call answering, and media streaming.
  Use this if you want a ready-to-run setup with minimal configuration.

---

## When to use this

Use this project if you want to:

* Build voice applications with Pipecat and Wavix
* Stream real-time audio over WebSockets
* Prototype or test telephony integration

---

## Wavix audio format

Wavix uses the following audio format:

* Format: PCM16
* Sample rate: 24 kHz
* Bit depth: 16-bit
* Frame size: 20 ms
* Channels: mono
* Endianness: little-endian

---

## Prerequisites

### Wavix account

Before you start, make sure you have:

* A Wavix account: https://docs.wavix.com/getting-started/create-account
* A Wavix phone number

### Environment variables

Set these environment variables:

* `WAVIX_API_KEY` – your Wavix API key
* `OPENAI_API_KEY` – your OpenAI API key
* `DEEPGRAM_API_KEY` – your Deepgram API key
* `CARTESIA_API_KEY` – your Cartesia API key

### System requirements

* Python 3.12
* `uv` package manager
* ngrok (for local development)

---

## Setup

Install the package:

```bash
pip install pipecat-wavix
```

If you use `uv`, add it to your `pyproject.toml`:

```toml
[project]
dependencies = [
    "pipecat-wavix>=1.0.0"
]
```

Then install dependencies:

```bash
uv sync
```

---

## Use with a Pipecat pipeline

Import the serializer:

```python
from pipecat_wavix import WavixFrameSerializer
```

Configure it:

```python
WAVIX_SAMPLE_RATE = 24000
BOT_SAMPLE_RATE = 16000

serializer = WavixFrameSerializer(
    stream_id=stream_id,
    params=WavixFrameSerializer.InputParams(
        wavix_sample_rate=WAVIX_SAMPLE_RATE,
        sample_rate=BOT_SAMPLE_RATE,
        audio_track="inbound",
    ),
)
```

Set up the transport:

```python
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

Outbound audio is automatically converted to Wavix’s required format: PCM16, 24 kHz, mono, little-endian, 20 ms frames.

---

## Run the example

### Option 1: run server.py (recommended)

Use `server.py` to handle the full Wavix call flow.

Run:

```bash
uv run server.py
```

The server:

* Starts a FastAPI app
* Opens an ngrok tunnel
* Exposes:

  * `POST /wavix/inbound` (webhook)
  * `GET /health` (health check)
  * `WS /ws` (WebSocket for media stream)

When a call reaches your Wavix number, the server:

* Answers the call
* Starts bidirectional streaming to `/ws`
* Launches the bot from `bot.py`

### What you’ll see

The server prints:

* Local URL (for example, `http://localhost:7860`)
* Public ngrok URL
* Webhook URL (`https://.../wavix/inbound`)
* WebSocket URL (`wss://.../ws`)

### Configure Wavix

Set your Wavix number’s voice webhook to:

```text
https://your-public-url/wavix/inbound
```

To update your number:

1. Sign in to your Wavix account.
2. Go to **Numbers & trunks** > **My numbers**.
3. Select your number.
4. Choose **Edit number**.
5. Paste the webhook URL.
6. Select **Save**.

Then call your number. The server handles the rest.

### Important

* Don’t start ngrok manually in this mode.
* Don’t call Wavix APIs manually — `server.py` handles everything.

---

## Run bot.py (standalone mode)

Use `bot.py` if you want to run the Pipecat development runner and manage Wavix calls yourself.

### What it does

* Starts the Pipecat development runner
* Accepts a WebSocket connection
* Parses the Wavix handshake
* Builds the audio pipeline

### What it doesn’t do

* Doesn’t answer calls
* Doesn’t create streams
* Doesn’t replace `server.py`

### Run the bot

```bash
uv run bot.py --transport wavix --proxy your-public-hostname
```

Example:

```bash
uv run bot.py --transport wavix --proxy your-ngrok-url.ngrok-free.dev
```

### Expose your local server

Start ngrok in a separate terminal:

```bash
ngrok http 7860
```

Use the ngrok hostname as the `--proxy` value (don’t include `https://`, unless required).

---

## Set up Wavix (standalone mode)

In this mode, you must handle call control yourself.

### Typical flow

1. Get the `call_id` from Wavix webhooks
   https://docs.wavix.com/api-reference/call-webhooks/on-call-event

2. Answer the call:

```bash
curl -L "https://api.wavix.com/v1/calls/$CALL_ID/answer" \
  -H "Authorization: Bearer $WAVIX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'
```

3. Start streaming:

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

---

## When to use each mode

Use `server.py` if you want:

* A complete, ready-to-run setup
* Automatic webhook handling
* Minimal configuration

Use `bot.py` if you want:

* Full control over call handling
* Integration with your own backend
* A development or testing setup

---
