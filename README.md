# Heartopia-Image-Painter

Simple GUI tool to "paint" an imported image onto the Heartopia in-game canvas by automating palette selection + clicks.

## Status

This is an early version focused on:

- Import any image
- Resize to the 1:1 preset (30x30)
- Select the on-screen canvas area (with translucent preview overlay)
- Configure palette button positions (main colors + shades)
- Paint by clicking cells in the selected area

## Setup

1. Create / activate a Python venv.
2. Install dependencies:

	- `pip install -r requirements.txt`

## Run

- `python main.py`

## Basic workflow

1. Import an image.
2. Select canvas preset: `1:1 (30x30)`.
3. Click **Select canvas area…** and drag a rectangle over the in-game canvas.
	- The app shows a translucent preview of your image while you drag.
4. Configure colors:
	- Click **Setup new color…**
	- If you have not set them yet, the wizard will first ask you to click:
	  - the **shades-panel** open button
	  - the **back** button (returns to main colors)
	- Then it will ask you to click:
	  - the **main color** button
	  - every **shade button** for that color (keep clicking shades until done)
	  - click **Finish** when you’re done capturing shades
5. Click **Paint now**.

## Safety

- This app controls your mouse and clicks on your screen.
- There is a short countdown before painting starts so you can focus the game window.
- PyAutoGUI failsafe is enabled: move the mouse to the **top-left corner** to abort.

## Configuration file

The app saves your palette/button coordinates to `config.json` in the repo folder.

It also persists your last selected preset, last imported image path, and last selected canvas rectangle.

## Notes / limitations

- Painting is currently a simple per-cell click approach.
- Color matching is currently "closest configured shade" (RGB distance). Better mapping and speedups can be added later.

