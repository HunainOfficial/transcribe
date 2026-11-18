# Cost-aware, speech-safe MAI transcription

Convert audio → compress **long dead-air only** (keep all speech) → Azure **MAI-Transcribe-2**.

**Goals (in order):**
1. Keep conversational words / important details  
2. Reduce billed minutes by shortening flat no-speech stretches  

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# ffmpeg on PATH required
```

`.env`:

```env
AZURE_SPEECH_KEY=...
AZURE_SPEECH_REGION=centralindia
AZURE_SPEECH_ENDPOINT=https://centralindia.api.cognitive.microsoft.com/
```

## Usage

```bash
# Default: smart pause compress + transcribe
python run.py 405

# Max accuracy, no cost save
python run.py 405 --mode off

# Preprocess only
python run.py 405 --skip-transcribe
```

Outputs in `out/`: `*.clean.wav`, `*.txt`, `*.json`

## Modes

| Mode | Behavior | Cost | Risk to words |
|------|----------|------|----------------|
| `smart` (default) | Keep all speech; cap dead-air ≥ 0.6s down to ~0.25s | ~10–15% on talky calls | Low |
| `off` | Convert only | None | None |
| `edges` | Trim start/end dead air only | Small | Very low |
| `long` | Legacy silenceremove | Higher save | Higher — avoid for sales calls |

On `405`, smart saved **~13%** minutes with **all key phrases kept** (including “once you pay 80%… stop”).  
If cleaning removes >30% duration, a **WARNING** is printed — spot-check payment details.

Note: earlier ~50% “savings” came from deleting real speech. True no-conversation dead-air on this call is closer to 10–15% if words are protected.

## What we learned

Aggressive silence deletion saved ~50% time but dropped critical lines
(e.g. “once you pay 80%, you stop”).  
`smart` shortens only detected no-conversation stretches and keeps speech segments intact.
