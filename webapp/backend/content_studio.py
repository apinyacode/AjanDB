"""
Extra "Data Generation" output modes beyond text (see book_compiler.py for
those): a single illustrative image, and a short narrated slideshow video,
both built from the same matched-source retrieval book_compiler.py already
does.

Image generation uses OpenAI's image API (DALL-E) - the only
already-integrated provider that offers one; Anthropic has none, so this
always needs an OpenAI key regardless of which provider the text modes are
using.

Video is NOT true AI video generation - no such provider (Runway, Pika,
Sora, ...) is wired into this app, and adding one would mean a brand-new
paid API this codebase has never touched. Instead it's a narrated
slideshow assembled locally with ffmpeg, from one generated image plus
Azure TTS narration (see tts.py) of a short script (see book_compiler.py's
"video_script" mode) - a real, working short vertical video, just not a
generated video clip.

Generated images/videos are non-deterministic per call (regenerating the
same instruction produces a different image), so unlike tts.py's/
convert.py's content-hash caches, nothing here is deduped - every call
saves a fresh file under a random id, and neither directory is ever
garbage-collected (same "no automatic cleanup" tradeoff already accepted
for data/images/ and data/sources/).
"""
import base64
import os
import subprocess
import uuid

from openai import OpenAI

from . import book_compiler, tts

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
IMAGES_DIR = os.path.join(_DATA_DIR, "generated_images")
IMAGES_URL_PREFIX = "/generated-images"
VIDEOS_DIR = os.path.join(_DATA_DIR, "generated_videos")
VIDEOS_URL_PREFIX = "/generated-videos"

_IMAGE_MODEL = "dall-e-3"
_IMAGE_SIZE = "1024x1792"  # tall aspect, closer to short-form vertical framing than square

# Vertical (9:16) frame matching TikTok/shorts-style video.
_VIDEO_WIDTH = 1080
_VIDEO_HEIGHT = 1920


def _build_image_prompt(instruction: str, sources: list[str]) -> str:
    context = f" Draw visual inspiration from these source materials: {', '.join(sources[:5])}." if sources else ""
    return (
        f"A respectful, editorial-style illustration for: {instruction}.{context} "
        "No embedded text or captions in the image itself."
    )


def generate_image(instruction: str, conn, api_key: str = None, max_sources: int = 5) -> dict:
    """Returns {"image_url": str, "prompt": str, "sources": [label, ...]}.
    Generates one illustrative image via OpenAI's image API, informed by
    the library's matched source material for `instruction` (used only to
    build the image prompt - the image itself is new, generated content,
    not a reproduction of anything in the library)."""
    if not api_key:
        raise RuntimeError(
            "Image generation needs an OpenAI API key - Anthropic has no image-generation "
            "API, so this always uses OpenAI regardless of which provider Data Generation's "
            "text modes are set to. Set one in the API Keys panel, or OPENAI_API_KEY in the "
            "backend's environment."
        )
    matches = book_compiler.retrieve_matches(instruction, conn, max_sources)
    sources = [m["label"] for m in matches]
    prompt = _build_image_prompt(instruction, sources)

    client = OpenAI(api_key=api_key)
    response = client.images.generate(
        model=_IMAGE_MODEL, prompt=prompt, size=_IMAGE_SIZE, response_format="b64_json", n=1,
    )
    image_bytes = base64.b64decode(response.data[0].b64_json)

    os.makedirs(IMAGES_DIR, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.png"
    with open(os.path.join(IMAGES_DIR, filename), "wb") as f:
        f.write(image_bytes)

    return {"image_url": f"{IMAGES_URL_PREFIX}/{filename}", "prompt": prompt, "sources": sources}


def _assemble_video(image_path: str, audio_path: str, output_path: str) -> None:
    """Builds a vertical mp4 from one still image (looped for the audio's
    full duration) and one narration track - see this module's docstring
    for why this is a locally-assembled slideshow, not AI video
    generation."""
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", image_path,
        "-i", audio_path,
        "-c:v", "libx264", "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-vf", f"scale={_VIDEO_WIDTH}:{_VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
               f"pad={_VIDEO_WIDTH}:{_VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2",
        "-shortest",
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg is not installed - video generation needs it on the server's PATH.")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed to assemble the video:\n{result.stderr[-2000:]}")


def generate_video(instruction: str, conn, text_provider: str = None, text_model: str = None,
                    text_api_key: str = None, image_api_key: str = None,
                    azure_speech_key: str = None, azure_speech_region: str = None,
                    max_sources: int = 10) -> dict:
    """Returns {"video_url": str, "script": str, "image_url": str,
    "sources": [label, ...]}. Writes a short narration script (see
    book_compiler.compile_book's "video_script" mode), generates one
    illustrative image, synthesises the script as speech (Azure TTS - see
    tts.py), and assembles the two into a vertical video with ffmpeg."""
    script_result = book_compiler.compile_book(
        instruction, conn, provider=text_provider, model=text_model, api_key=text_api_key,
        mode="video_script", max_sources=max_sources,
    )
    script = script_result["markdown"].strip()

    image_result = generate_image(instruction, conn, api_key=image_api_key, max_sources=max_sources)
    image_path = os.path.join(IMAGES_DIR, os.path.basename(image_result["image_url"]))

    audio_url = tts.get_or_synthesize(script, region=azure_speech_region, api_key=azure_speech_key)
    audio_path = os.path.join(tts.AUDIO_DIR, os.path.basename(audio_url))

    os.makedirs(VIDEOS_DIR, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.mp4"
    output_path = os.path.join(VIDEOS_DIR, filename)
    _assemble_video(image_path, audio_path, output_path)

    return {
        "video_url": f"{VIDEOS_URL_PREFIX}/{filename}",
        "script": script,
        "image_url": image_result["image_url"],
        "sources": script_result["sources"],
    }
