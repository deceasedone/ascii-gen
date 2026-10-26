# ASCII Gen

Turns images, GIFs and videos into coloured ASCII art. Every character is coloured
with the hex colour sampled from the original. Images come out as PNG (or copied as
text), GIFs as GIF or MP4, and videos as MP4 with the original audio, or as GIF.

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://localhost:8000.

There is also a CLI. The output extension picks the format, and no output prints to the terminal in colour:

```bash
python cli.py photo.jpg
python cli.py photo.jpg out.png --mode braille --theme synthwave --focus hide
python cli.py photo.jpg out.txt --columns 120
python cli.py photo.jpg out.html
python cli.py photo.jpg out.ans
python cli.py photo.jpg --chat braille --columns 40
python cli.py anim.gif out.gif --mode text --text "hello "
python cli.py clip.mp4 out.mp4 --background tinted
python cli.py clip.mp4 --play
```

## How it works

| Step | File |
|---|---|
| Glyph atlas: ASCII and custom characters drawn from a monospace font; block (`▀▄▌▐▚…`), shade (`░▒▓`) and braille (`⠁…⣿`) glyphs drawn in code | `asciigen/glyphs.py` |
| Focus: U²-Net-small (4.7 MB ONNX model, the one rembg uses) finds the main subject; the background is dimmed or removed and contrast is stretched on the subject only | `asciigen/focus.py` |
| Themes: gradients map brightness to a colour ramp (with an optional accent for blue hues), palettes snap colours to the nearest swatch in Oklab | `asciigen/themes.py` |
| Image → grid: each cell is sampled at 6×12 sub-pixels in linear light. Smooth cells get a ramp character (` .:-=+*#%@`); cells with edges/texture are matched against every glyph's shape | `asciigen/converter.py` |
| Colour: each character takes the average colour of the pixels its strokes cover; the tinted background takes the colour of the rest of the cell | `asciigen/converter.py` |
| Rendering: glyph masks are tiled with NumPy and tinted with each cell's colour | `asciigen/converter.py` |
| Video/GIF: ffmpeg decodes frames, which are converted in parallel threads, then encoded to x264 MP4 (original audio muxed back) or a palette-optimised GIF. A character only changes between frames when the new one is clearly better, which reduces flicker | `asciigen/video.py` |
| Web app: FastAPI; images convert synchronously, videos run as background jobs with progress polling | `app/main.py`, `app/static/` |

### Settings

- **Style:** `ascii` (ASCII chosen by shape, the default), `hybrid` (ASCII plus braille/blocks for detail), `braille` (dot matrix), `ramp` (one character per brightness level: simple, detailed 70-character, shading blocks, binary or your own characters), `text` (the picture is filled with a phrase)
- **Theme:** original colours, or grayscale, matrix, amber, glitch, synthwave, crypt, Game Boy, paper
- **Focus:** off, dim background, hide background
- **Saturation:** 1.0 keeps the source colours; higher makes them more vivid
- **Columns:** characters per row, 40–500 (default 500); more columns means more detail
- **Cell size:** font size in px; sets the output resolution
- **Edge tracing:** how readily edges get shape-matched characters, and how freely braille/blocks are used
- **Brightness:** brightens colours without shifting hue; 1.0 keeps the exact source colours
- **Background:** `black` (classic, high contrast) or `tinted` (a dim version of each cell's colour fills the gaps, so it looks closer to the photo)

## Deploy

The Docker image includes everything it needs: ffmpeg comes with `imageio-ffmpeg`, the font with `fonts-dejavu-core`,
and the focus model is downloaded and checksum-verified at build time. Running locally, it is downloaded on first use to
`~/.cache/asciigen` (override with `ASCII_MODEL_DIR`).

```bash
docker build -t ascii-gen .
docker run -p 8000:8000 ascii-gen
```

Works on Render, Railway, Fly.io, Google Cloud Run or a Hugging Face Space (Docker SDK). The container listens on `$PORT`.

Environment settings (defaults in brackets): `MAX_UPLOAD_MB` (100), `MAX_VIDEO_SECONDS` (0 = no limit),
`MAX_VIDEO_WIDTH` (1920), `MAX_IMAGE_PIXELS` (40M), `VIDEO_WORKERS` (1 render at a time),
`FRAME_WORKERS` (CPU count, max 8: threads per render).

Uploaded images are never written to disk. Uploaded videos and GIFs are deleted as soon as their render finishes.
Rendered files are deleted after an hour by a sweep that runs every 10 minutes. Restarting the server clears all
job files, and a failed job's files are removed straight away. Jobs are tracked in memory, so run a single instance.
Use a queue (Redis + RQ/Celery) and object storage if you need to scale out.
