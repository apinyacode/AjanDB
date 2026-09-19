"""
Server-side text-to-speech via Azure Speech, replacing the review UI's
"Read Aloud" feature's dependence on the browser's own `SpeechSynthesisUtterance`
- voice availability/quality for that varies wildly by OS/browser (especially
for Thai), so every tester hears the same voice this way instead.

Generated audio is cached on disk by a hash of the input text (the same
dedup pattern convert.py's `_save_image_as_jpg` already uses for images),
so reading the same paragraph twice - e.g. a reviewer replaying a page -
never re-hits the billed API a second time.

Voice is picked automatically from the text itself (any Thai-script
character selects the Thai neural voice, otherwise English) rather than
needing the caller to pass a language hint - this app's pages are
routinely mixed English/Thai per paragraph, and detecting from the actual
characters is simpler and more reliable than trusting a hint.
"""
import hashlib
import os
import re

import requests

AUDIO_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "audio"
)
AUDIO_URL_PREFIX = "/audio"

_THAI_CHAR_RE = re.compile(r"[฀-๿]")
_VOICE_THAI = "th-TH-PremwadeeNeural"
_VOICE_ENGLISH = "en-US-JennyNeural"

_OUTPUT_FORMAT = "audio-16khz-64kbitrate-mono-mp3"


def _voice_for_text(text: str) -> str:
    return _VOICE_THAI if _THAI_CHAR_RE.search(text) else _VOICE_ENGLISH


def _ssml(text: str, voice: str) -> str:
    escaped = (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    lang = "th-TH" if voice == _VOICE_THAI else "en-US"
    return (
        f'<speak version="1.0" xml:lang="{lang}">'
        f'<voice name="{voice}">{escaped}</voice></speak>'
    )


def _get_access_token(region: str, api_key: str) -> str:
    resp = requests.post(
        f"https://{region}.api.cognitive.microsoft.com/sts/v1.0/issueToken",
        headers={"Ocp-Apim-Subscription-Key": api_key},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.text


def synthesize(text: str, region: str = None, api_key: str = None) -> bytes:
    """Calls Azure Speech and returns MP3 bytes for `text`. Raises
    RuntimeError if no key/region is configured (matching the "no API key"
    graceful-error pattern used elsewhere in this app, e.g. the vision-LLM
    engine), or requests.HTTPError if Azure itself rejects the call."""
    region = region or os.environ.get("AZURE_SPEECH_REGION")
    api_key = api_key or os.environ.get("AZURE_SPEECH_KEY")
    if not region or not api_key:
        raise RuntimeError(
            "Azure Speech isn't configured - set an Azure Speech key/region "
            "in the API Keys panel, or AZURE_SPEECH_KEY/AZURE_SPEECH_REGION "
            "in the backend's environment.")

    token = _get_access_token(region, api_key)
    voice = _voice_for_text(text)
    resp = requests.post(
        f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": _OUTPUT_FORMAT,
            "User-Agent": "AjanDB",
        },
        data=_ssml(text, voice).encode("utf-8"),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def _cache_path(text: str) -> tuple[str, str]:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    filename = f"{digest}.mp3"
    return os.path.join(AUDIO_DIR, filename), f"{AUDIO_URL_PREFIX}/{filename}"


def get_or_synthesize(text: str, region: str = None, api_key: str = None) -> str:
    """Returns the URL path to `text`'s audio, generating and caching it via
    Azure only on a cache miss - repeat requests for the same paragraph
    never re-hit the API."""
    path, url = _cache_path(text)
    if os.path.exists(path):
        return url
    audio_bytes = synthesize(text, region=region, api_key=api_key)
    os.makedirs(AUDIO_DIR, exist_ok=True)
    with open(path, "wb") as f:
        f.write(audio_bytes)
    return url
