# LoopCap

LoopCap is a Linux-native GIF screen recorder focused on Fedora.

## Inspiration

LoopCap is inspired by the idea behind the Windows app LICEcap:

- small desktop utility
- quick screen capture workflow
- direct GIF output

This project does not use code from the original LICEcap repository. It is an original implementation built for Fedora and modern Wayland-based Linux desktops.

## Current status

The project now contains:

- a compact GTK control widget
- a real `xdg-desktop-portal` screen-share flow
- GIF saving through PipeWire + GStreamer + FFmpeg
- pause, resume, and stop controls
- local `venv` instructions that do not modify the global Python install

Current limitation:

- the app saves a GIF of the selected portal source
- the FPS is configurable in the UI
- it still does not crop to a custom capture rectangle yet

## Goal

Build a small desktop app that:

- records short visual loops quickly
- exports directly to GIF
- works on Fedora, including modern Wayland sessions
- eventually supports region-based capture similar in spirit to LICEcap

## Why this differs from Windows LICEcap

Fedora on Wayland changes the capture model:

- on X11, region capture is straightforward
- on Wayland, direct screen capture is restricted by design
- the Linux-native approach is to use `xdg-desktop-portal` + PipeWire and crop the chosen stream later in the pipeline

So LoopCap should be treated as a Fedora-first implementation inspired by LICEcap, not a direct port of the Windows codebase.

## Recommended stack

### App shell

- Python 3.13
- GTK 4 via PyGObject

### Capture pipeline

- `xdg-desktop-portal`
- PipeWire
- GStreamer `pipewiresrc`

### Encoding

- GStreamer `pngenc` + `multifilesink`
- FFmpeg palette-based GIF assembly

## Local environment

The system Python detected here is `Python 3.13.14`.

Create a local virtual environment that reuses Fedora's system GTK bindings:

```bash
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python -m ensurepip --upgrade
sh run.sh
```

## Fedora packages you may need

If GTK bindings are missing, install the distro packages rather than using `pip`:

```bash
sudo dnf install python3-gobject gtk4
```

For recording support:

```bash
sudo dnf install pipewire xdg-desktop-portal xdg-desktop-portal-gnome \
  gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-base
```

## What works today

1. The app opens as a compact floating control widget.
2. You can choose the GIF FPS before starting.
3. Clicking play opens the Fedora screen-share dialog.
4. During recording, the widget shows pause and stop controls.
5. You can stop either from the app stop button or with `sh stop.sh` without reopening the window.
6. The GIF is finalized from temporary PNG frames and the status line confirms the save.

## Save location

GIFs are currently written to:

```text
~/Videos/LoopCap/
```

## Remote stop

While a recording is running, you can stop it without bringing the window to the front:

```bash
sh stop.sh
```

## What is next

1. Crop frames to a custom capture rectangle instead of saving the full selected source.
2. Improve GIF quality and file size controls.
3. Add more polish around pause/resume and shortcuts.
4. Prepare the project for a public GitHub repository.
